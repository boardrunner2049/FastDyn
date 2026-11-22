# Relink the exact objects of the normal upstream firmware with ELF relocations.
$(BUILD)/firmware-relocs.elf: $(BUILD)/firmware.elf
	$(CC) $(LDFLAGS) -Wl,--emit-relocs -o $@ $(OBJ) $(LIBS)
