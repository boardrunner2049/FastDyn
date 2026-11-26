"""Independent admission and machine-output checks for ordinary exit retirement."""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

import scopt.link
from scopt.canon import fnv1a64
from scopt.decode.search import SearchRow, encode_search
from scopt.decode.x86 import sweep
from scopt.formats.enums import Encoding, OpMarkKind, SiteKind
from scopt.formats.layouts import SC_MAGIC, SC_RECORD_COMMIT
from scopt.formats.sc_records import ScHeader, ScRecord
from scopt.formats.scm_v1 import RunInfo, ScmInsn, ScmOpMark, ScmSite, ScmStaticExit, TbChunk
from scopt.ordinary_exit import RUNTIME, capture, retire, retired_slots, targets
from scopt.passes.ordinary_exit import observed_installations
from scopt.validate import Validation, _ordinary_exit_projection

CA = 0x7FFF78000000
TB = 0x40
TC = 0x100
AT = 7
END = 24
EPILOGUE = CA + 22
RETURN = CA + 24
UNUSED = 0xFFFF

def sha(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def disp(target, after):
    return (target-after).to_bytes(4,'little',signed=True)


def code_and_sites():
    tc = CA+TC
    lea_value, ret_value = CA+TB, RETURN
    lea_field = disp(lea_value, tc+AT+12)
    ret_field = disp(ret_value, tc+END)
    code = (bytes.fromhex('b8112233446690e900000000488d05') + lea_field +
            b'\xe9'+ret_field+bytes.fromhex('31c9'))
    sites = (
        ScmSite(AT+1,AT,4,Encoding.GOTO_TB_REL32,SiteKind.GOTO_TB,0,tc+AT+5,1,0),
        ScmSite(AT+8,AT+5,4,Encoding.RIPREL32,SiteKind.MOVI_EXIT_TB_PTR,0,lea_value,1,
                int.from_bytes(lea_field,'little')),
        ScmSite(AT+13,AT+12,4,Encoding.REL32,SiteKind.EXIT_TB_RET_JMP,0,ret_value,1,
                int.from_bytes(ret_field,'little')),
    )
    return code, sites


@pytest.fixture
def ordinary():
    code, sites = code_and_sites()
    insns = (
        ScmInsn((0x70,0,0),0,AT),
        ScmInsn((0x72,0,0),AT,END),
        ScmInsn((0x74,0,0),END,len(code)),
    )
    search = encode_search([SearchRow(i.words,i.host_end) for i in insns],pcrel=True)
    record = ScRecord(TB, TC+len(code)+len(search), TC,len(code),0,0x800400,0x8000000,
                      0xffffffffffffffff,1,0x110,0xff020000,6,3,
                      (AT+5,UNUSED),(AT+1,UNUSED),0,SC_RECORD_COMMIT).with_checksum()
    marks = (
        ScmOpMark(AT,OpMarkKind.GOTO_TB,1), ScmOpMark(AT+5,OpMarkKind.EXIT_TB,1),
    )
    exits = (ScmStaticExit(0,1,1,0x08000090),)
    chunk = TbChunk(0,1,record.to_bytes(),0x08000070,fnv1a64(code+search),1,0,0,
                    len(code),len(code),0xffffffff,len(code),insns=insns,
                    sites=sites,opmarks=marks,static_exits=exits,guest_bytes=b'\x00\xbf'*3)
    return SimpleNamespace(uid='source',slot=0,origin='elf_verified',reloc=SimpleNamespace(relocatable=True),
        stub_start=None,native_fragments=(),tc_offset=TC,tb_offset=TB,code=code,search=search,
        record=chunk.record,pool=(),pool_start=None,body_end=len(code),ldst_end=len(code),
        recovery=(),insns=tuple(SimpleNamespace(index=i,host_start=x.host_start,host_end=x.host_end)
                                for i,x in enumerate(insns))),chunk


@pytest.fixture
def provenance():
    prologue = bytes.fromhex(
        '55534154415541564157488bef4881c478fbffffffe633c04881c488040000c5f877415f415e415d415c5b5dc3'
    )
    header = ScHeader(SC_MAGIC,7,80,0x10000,CA,len(prologue),1,0x51,4096,10,152,0,0)
    snapshot = SimpleNamespace(header=header,snapshot_id='baseline',runtime={
        'qemu_sha256':RUNTIME,'prologue_sha256':sha(prologue),'runtime_signature':'0x51',
        'prologue_end':'0x2d','code_address':hex(CA),'code_size':'0x10000',
    })
    info = RunInfo(1,1,CA,0x10000,len(prologue),24,22,0x51,0,0,0,64,152,4096,10,3,0xd06f7,0,0,0,0,0,
                   prologue)
    return snapshot, info


def captured(info, bindings=None):
    return SimpleNamespace(captures=(SimpleNamespace(chunks=(SimpleNamespace(body=info),)),),
                           slots=bindings or {})


def transform(unit, chunk, extra=(), selected=(0,)):
    return retire(unit,chunk,selected,CA,EPILOGUE,RETURN,targets((unit,*extra),CA))


def test_genuine_retirement_has_truthful_v7_and_scm(ordinary,provenance):
    unit,chunk = ordinary
    snapshot,info = provenance
    assert capture(snapshot,captured(info)) == (EPILOGUE,RETURN)
    payload,derived = transform(unit,chunk)
    output = payload[:len(unit.code)]
    assert (output[:AT],output[END:],payload[len(unit.code):]) == (
        unit.code[:AT],unit.code[END:],unit.search)
    decoded = sweep(output[AT:END],CA+TC+AT)
    assert [i.branch_target for i in decoded[:2]] == [EPILOGUE,EPILOGUE]
    assert b''.join(i.raw for i in decoded[2:]) == b'\x90'*7
    record = ScRecord.from_bytes(derived.record)
    original = ScRecord.from_bytes(unit.record)
    assert record == original.replace(jmp_insn_offset=(UNUSED,UNUSED),
                                      jmp_reset_offset=(UNUSED,UNUSED)).with_checksum()
    assert record.checksum_ok and retired_slots(unit.record,derived.record) == (0,)
    assert derived.code_fnv64 == fnv1a64(payload)
    assert (derived.insns,derived.guest_bytes,derived.code_size,derived.tb_chunk_version) == (
        chunk.insns,chunk.guest_bytes,chunk.code_size,chunk.tb_chunk_version)
    assert derived.static_exits == (replace(chunk.static_exits[0],slot=255,direct=0),)
    assert [(s.insn_off,s.field_off,s.kind,s.value,s.final_field) for s in derived.sites] == [
        (at,at+1,SiteKind.EXIT_TB_EPI_JMP,EPILOGUE,int.from_bytes(output[at+1:at+5],'little'))
        for at in (AT,AT+5)]
    assert [(m.host_off,m.kind) for m in derived.opmarks] == [
        (AT,OpMarkKind.EXIT_TB),(AT+5,OpMarkKind.EXIT_TB)]


@pytest.mark.parametrize('delta',[5,8])
def test_foreign_destination_cannot_inherit_reset_exemption(ordinary,delta):
    unit,chunk = ordinary
    foreign_tc = 0x200
    foreign = SimpleNamespace(tc_offset=foreign_tc, pool_start=None, ldst_end=5,pool=(),
        code=b'\xe9'+disp(CA+TC+AT+delta,CA+foreign_tc+5))
    with pytest.raises(ValueError,match='interior|reference|entry'):
        transform(unit,chunk,(foreign,))


def test_foreign_rip_read_straddling_span_cannot_survive(ordinary):
    unit,chunk = ordinary
    foreign_tc = 0x200
    # The four-byte reference begins two bytes outside the edited region.
    foreign = SimpleNamespace(tc_offset=foreign_tc,pool_start=None,ldst_end=6,pool=(),
        code=b'\x8b\x05'+disp(CA+TC+AT-2,CA+foreign_tc+6))
    with pytest.raises(ValueError,match='interior|reference'):
        transform(unit,chunk,(foreign,))


def test_recovery_point_at_exit_is_a_barrier(ordinary):
    unit,chunk = ordinary
    unit.recovery = (SimpleNamespace(host_return_offset=AT+5),)
    with pytest.raises(ValueError,match='interior|recovery|observer'):
        transform(unit,chunk)


@pytest.mark.parametrize('offset', [AT,AT+8,AT+13])
def test_wrong_native_preimage_or_target_fails_even_with_matching_capture_hash(ordinary,offset):
    unit,chunk = ordinary
    code = bytearray(unit.code)
    code[offset] ^= 1
    unit.code = bytes(code)
    bound_wrong = replace(chunk,code_fnv64=fnv1a64(unit.code+unit.search))
    with pytest.raises(ValueError,match='preimage|uncanonical|unexpected|reference|target'):
        transform(unit,bound_wrong)


def test_unrelated_operation_in_exit_span_is_refused(ordinary):
    unit,chunk = ordinary
    overlap = ScmOpMark(AT+2,OpMarkKind.QEMU_LD,1)
    with pytest.raises(ValueError,match='opmark'):
        transform(unit,replace(chunk,opmarks=chunk.opmarks+(overlap,)))


def test_static_projection_rejects_unrelated_output_bytes(monkeypatch,ordinary,provenance):
    unit,chunk = ordinary
    payload,derived = transform(unit,chunk)
    snapshot,info = provenance
    snapshot.units = lambda: (unit,)
    snapshot.unit = lambda uid: unit
    j = captured(info,{unit.slot:SimpleNamespace(bound=True,chunk=chunk)})
    monkeypatch.setattr(scopt.link,'capture_join',lambda _:j)
    bad = SimpleNamespace(uid=unit.uid,slot=unit.slot,moved=False,patched=(),
                          record=derived.record,body=bytes((payload[0]^1,))+payload[1:])
    validation = Validation()
    assert _ordinary_exit_projection(snapshot,SimpleNamespace(units=(bad,)),validation) == ({},{})
    assert any(r['code']=='V12' and r['result']=='fail' and 'more than' in r['detail']
               for r in validation.rows)


@pytest.mark.parametrize('mutation',['offsets','nonzeroing'])
def test_capture_declines_counterfeit_prologue(provenance,mutation):
    snapshot,info = provenance
    if mutation=='offsets':
        info = replace(info,epilogue_off=24,tb_ret_off=26)
    else:
        prologue = info.prologue[:22]+b'\x90\x90'+info.prologue[24:]
        info = replace(info,prologue=prologue)
        snapshot.runtime['prologue_sha256'] = sha(prologue)
    with pytest.raises(ValueError,match='epilogue|prologue|canonical|zero|clear EAX'):
        capture(snapshot,captured(info))


@pytest.fixture
def trace_fixture(tmp_path):
    units = tuple(SimpleNamespace(uid=uid,tc_offset=off) for uid,off in (('u',TC),('v',0x200)))
    snap = SimpleNamespace(header=SimpleNamespace(code_address=CA),snapshot_id='baseline',
                         runtime={'qemu_sha256':RUNTIME},units=lambda:units)
    profile = SimpleNamespace(profile_id='exact-profile')
    trace = tmp_path/'train.log'
    raw = f'Trace 0: other row\nLinking TBs {CA+TC:#x} index 0 -> {CA+0x200:#x}\n'.encode()
    trace.write_bytes(raw)
    path = tmp_path/'manifest.json'
    vals = {'schema':'scopt.ordinary_exit_training/1','input_snapshot_id':'baseline',
            'profile_id':profile.profile_id,'qemu_sha256':RUNTIME,'trace':'train.log',
            'trace_sha256':sha(raw),'steady_lines':[1,2]}
    return snap,profile,path,trace,vals


def test_manifest_binds_exact_training_bytes(trace_fixture):
    snap,profile,path,trace,vals = trace_fixture
    path.write_text(json.dumps(vals))
    links,digest = observed_installations(snap,profile,path)
    assert links == {('u',0):{'v'}} and digest == sha(trace.read_bytes())


@pytest.mark.parametrize(('field','descriptor','message'),[
    ('schema','unsupported','manifest'),
    ('input_snapshot_id','other','identity'),
    ('qemu_sha256','other','identity'),
    ('profile_id','other','identity'),
    ('trace_sha256','sha256:'+'0'*64,'SHA-256'),
    ('steady_lines',[True,2],'bounds'),
    ('steady_lines',[2,1],'bounds'),
    ('steady_lines',[1,4],'bounds'),
])
def test_training_manifest_malformed(trace_fixture,field,descriptor,message):
    snap,profile,path,_,vals = trace_fixture
    vals[field] = descriptor
    path.write_text(json.dumps(vals))
    with pytest.raises(ValueError,match=message):
        observed_installations(snap,profile,path)


@pytest.mark.parametrize('mutant',['unknown_tb','malformed_link'])
def test_declines_link_lines_without_valid_exact_bindings(trace_fixture,mutant):
    snap,profile,path,trace,vals = trace_fixture
    wrong = (f'Linking TBs {CA+TC:#x} index 0 -> 0x123\n' if mutant=='unknown_tb' else
             f'Linking TBs {CA+TC:#x} index 2 -> {CA+0x200:#x}\n').encode()
    trace.write_bytes(wrong)
    vals.update(trace_sha256=sha(wrong),steady_lines=[1,1])
    path.write_text(json.dumps(vals))
    with pytest.raises(ValueError,match='unknown TB|invalid.*link|malformed.*link'):
        observed_installations(snap,profile,path)
