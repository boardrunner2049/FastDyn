import logging
import os
from elftools.elf.elffile import ELFFile
from elftools.dwarf.dwarf_expr import DWARFExprParser

log = logging.getLogger(__name__)

# These match the FieldType enum in your C plugin exactly
FIELD_UINT32 = 0
FIELD_UINT16 = 1
FIELD_UINT8 = 2
FIELD_POINTER = 3
FIELD_STRING_INLINE = 4
FIELD_STRING_PTR = 5

class SchemaGenerator:
    def __init__(self, binary_path):
        self.binary_path = binary_path
        with open(binary_path, 'rb') as f:
            elf = ELFFile(f)
            if not elf.has_dwarf_info():
                raise ValueError("[!] Target binary does not contain DWARF debug symbols.")
            self.dwarf = elf.get_dwarf_info()

        # Cache DIEs by their offset for fast type lookups
        self.die_cache = self._build_die_cache()

    def _build_die_cache(self):
        """Indexes all DIEs to easily follow DW_AT_type references."""
        cache = {}
        for cu in self.dwarf.iter_CUs():
            for die in cu.iter_DIEs():
                cache[die.offset] = die
        return cache

    def generate_schema(self, target_structs, target_symbols):
        """Extract the complete definition for each requested DWARF structure.

        A C compiler may emit a declaration-only DIE before the real
        definition in another compilation unit.  Taking the first matching
        name produces an empty schema for common RTOS types such as FreeRTOS'
        ``tskTaskControlBlock``.  Retain the candidate with the richest
        flattened field set instead.
        """
        wanted = set(target_structs)
        parsed_structs = {}

        # Hunt down target structures. A later, complete definition replaces
        # an earlier forward declaration (or a less complete duplicate).
        for cu in self.dwarf.iter_CUs():
            for die in cu.iter_DIEs():
                if die.tag != 'DW_TAG_structure_type' or 'DW_AT_name' not in die.attributes:
                    continue
                struct_name = die.attributes['DW_AT_name'].value.decode('utf-8')
                if struct_name not in wanted:
                    continue
                fields = self._flatten_struct(die)
                previous = parsed_structs.get(struct_name)
                if previous is None or len(fields) > len(previous):
                    parsed_structs[struct_name] = fields

        output_lines = []
        for struct_name in target_structs:
            fields = parsed_structs.get(struct_name)
            if fields is None:
                continue
            output_lines.append(f"STRUCT {struct_name} {len(fields)}")
            for field in fields:
                output_lines.append(f"{field['name']} {field['offset']} {field['size']} {field['type']}")

        # Add global symbols after structures so the native loader can process
        # the schema in one streaming pass.
        for sym_name, sym_addr in target_symbols.items():
            output_lines.append(f"SYMBOL {sym_name} {hex(sym_addr)}")

        return "\n".join(output_lines) + "\n"

    def _flatten_struct(self, struct_die, prefix="", base_offset=0):
        """Recursively flattens fields and calculates absolute offsets."""
        fields = []

        for child in struct_die.iter_children():
            if child.tag == 'DW_TAG_member':
                name_attribute = child.attributes.get('DW_AT_name')
                # Anonymous C structs/unions are common in modern RTOS task
                # control blocks. They have no field name of their own, but
                # their named children still form useful inspectable fields.
                field_name = (
                    name_attribute.value.decode('utf-8') if name_attribute else ""
                )
                full_name = f"{prefix}{field_name}".replace("..", ".")

                # Get relative offset and convert to absolute
                if 'DW_AT_data_member_location' in child.attributes:
                    rel_offset = self._member_offset(
                        child.attributes['DW_AT_data_member_location'].value
                    )
                else:
                    rel_offset = 0 # Sometimes 0 offset is implied

                abs_offset = base_offset + rel_offset

                # Resolve the underlying type
                if 'DW_AT_type' in child.attributes:
                    type_offset = child.attributes['DW_AT_type'].value
                    # pyelftools type offsets are relative to the CU, we need the absolute offset in the DWARF info
                    cu_offset = struct_die.cu.cu_offset
                    target_die = self.die_cache.get(cu_offset + type_offset)

                    if target_die:
                        # Follow typedefs to the base type
                        base_die = self._get_base_type(target_die)

                        if base_die and base_die.tag in {
                            'DW_TAG_structure_type', 'DW_TAG_union_type'
                        }:
                            # It's an inline nested struct! Recurse.
                            nested_prefix = f"{full_name}." if full_name else prefix
                            nested = self._flatten_struct(base_die, nested_prefix, abs_offset)
                            fields.extend(nested)
                            continue

                        # A nameless scalar member is padding or an unnamed
                        # implementation detail with no stable UI label.
                        if not full_name:
                            continue

                        # Map to C Plugin FieldType
                        c_type, size = self._map_to_c_type(base_die, child)
                        fields.append({
                            "name": full_name,
                            "offset": abs_offset,
                            "size": size,
                            "type": c_type
                        })
        return fields

    def _member_offset(self, value):
        """Return a constant DWARF member offset.

        GCC may encode a member offset either as a plain integer or as a
        DWARF expression such as ``DW_OP_plus_uconst 4``.  The latter is
        common in ARMv7-A objects (including RT-Thread's upstream QEMU BSP)
        and pyelftools represents it as a ``ListContainer``.  Schema output
        is necessarily static, so accept only expressions that evaluate to a
        single constant and fail clearly for dynamic locations.
        """
        if isinstance(value, int):
            return value

        operations = DWARFExprParser(self.dwarf.structs).parse_expr(value)
        if len(operations) == 1 and operations[0].op_name in {
            "DW_OP_plus_uconst",
            "DW_OP_constu",
            "DW_OP_consts",
        }:
            return int(operations[0].args[0])

        raise ValueError(
            "Unsupported non-constant DWARF member location: "
            f"{operations!r}"
        )

    def _get_base_type(self, die):
        """Follows DW_AT_type chains through typedefs/const/volatile to find the actual type."""
        current_die = die
        while current_die and current_die.tag in ('DW_TAG_typedef', 'DW_TAG_const_type', 'DW_TAG_volatile_type'):
            if 'DW_AT_type' not in current_die.attributes:
                return None
            type_offset = current_die.attributes['DW_AT_type'].value
            current_die = self.die_cache.get(current_die.cu.cu_offset + type_offset)
        return current_die

    def _map_to_c_type(self, base_die, member_die):
        """Heuristics to map DWARF types to the  C engine enum."""
        if not base_die:
            return FIELD_UINT32, 4 # Fallback

        if base_die.tag == 'DW_TAG_pointer_type':
            # Check if it's a char* (String pointer)
            if 'DW_AT_type' in base_die.attributes:
                ptr_target = self.die_cache.get(base_die.cu.cu_offset + base_die.attributes['DW_AT_type'].value)
                ptr_base = self._get_base_type(ptr_target)
                if ptr_base and ptr_base.tag == 'DW_TAG_base_type':
                    name = ptr_base.attributes.get('DW_AT_name', lambda: None)
                    if name and b'char' in name.value:
                        return FIELD_STRING_PTR, 4
            return FIELD_POINTER, 4

        elif base_die.tag == 'DW_TAG_array_type':
            # Check if it's an inline char array (e.g., pcTaskName[16])
            size = self._calculate_array_size(base_die)
            return FIELD_STRING_INLINE, size

        elif base_die.tag == 'DW_TAG_base_type':
            size = base_die.attributes.get('DW_AT_byte_size', None)
            size_val = size.value if size else 4
            if size_val == 1:
                return FIELD_UINT8, 1
            elif size_val == 2:
                return FIELD_UINT16, 2
            else:
                return FIELD_UINT32, 4

        return FIELD_UINT32, 4 # Default fallback

    def dump_all_struct_names(self):
        """Prints every struct name found in the DWARF info to help find the right targets."""
        found_structs = set()
        for cu in self.dwarf.iter_CUs():
            for die in cu.iter_DIEs():
                if die.tag == 'DW_TAG_structure_type' and 'DW_AT_name' in die.attributes:
                    name = die.attributes['DW_AT_name'].value.decode('utf-8')
                    found_structs.add(name)

        log.info(f"[*] Found {len(found_structs)} unique structs in DWARF:")
        for name in sorted(found_structs):
            log.info(f"  - {name}")

    def _calculate_array_size(self, array_die):
        """Calculates total byte size of an array from its subrange."""
        for child in array_die.iter_children():
            if child.tag == 'DW_TAG_subrange_type' and 'DW_AT_upper_bound' in child.attributes:
                # Array size is usually upper_bound + 1
                return child.attributes['DW_AT_upper_bound'].value + 1
        return 16 # Fallback size
