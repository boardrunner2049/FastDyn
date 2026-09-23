"""Opt-in real FMI3 package test. Set FASTDYN_TEST_RUMOCA_DIR to Rumoca 0.10.1."""
import ctypes as C
import math
import os
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

import pytest

from fastdyn.fmu_build import FmuBuild, build_fmu


@pytest.mark.skipif(not os.environ.get('FASTDYN_TEST_RUMOCA_DIR'), reason='requires audited Rumoca and FMI3 headers')
def test_source_fmu_packages_and_simulates(tmp_path):
    source = tmp_path / 'ContinuousProbe.mo'
    source.write_text('model ContinuousProbe\ninput Real u(start=0);\noutput Real x(start=0,fixed=true);\nequation\nder(x)=u-x;\nend ContinuousProbe;\n')
    repo = Path(__file__).resolve().parents[2]
    build = FmuBuild(name='probe', model='ContinuousProbe', model_file=source,
                     source_root=source, extra_source_roots=(), output=tmp_path/'fmi3',
                     repo_root=repo, compiler_root=Path(os.environ['FASTDYN_TEST_RUMOCA_DIR']))
    build_fmu(build, update_submodules_first=False)
    with zipfile.ZipFile(build.fmu_path) as archive:
        assert 'modelDescription.xml' in archive.namelist()
        assert any(p.startswith('binaries/') and p.endswith('.so') for p in archive.namelist())
        root = ET.fromstring(archive.read('modelDescription.xml'))
    package = build.output / build.model
    library = C.CDLL(str(next((package/'binaries').rglob('*.so'))))
    refs = {v.attrib['name']: int(v.attrib['valueReference']) for v in root.find('ModelVariables')}
    token = root.attrib['instantiationToken'].encode()
    instantiate = library.fmi3InstantiateCoSimulation
    instantiate.restype = C.c_void_p
    instantiate.argtypes = [C.c_char_p,C.c_char_p,C.c_char_p,C.c_bool,C.c_bool,C.c_bool,C.c_bool,
                            C.POINTER(C.c_uint32),C.c_size_t,C.c_void_p,C.c_void_p,C.c_void_p]
    enter = library.fmi3EnterInitializationMode
    enter.argtypes = [C.c_void_p,C.c_bool,C.c_double,C.c_double,C.c_bool,C.c_double]
    leave = library.fmi3ExitInitializationMode
    leave.argtypes = [C.c_void_p]
    set_values = library.fmi3SetFloat64
    get_values = library.fmi3GetFloat64
    for function in (set_values, get_values):
        function.argtypes = [C.c_void_p,C.POINTER(C.c_uint32),C.c_size_t,C.POINTER(C.c_double),C.c_size_t]
    step = library.fmi3DoStep
    step.argtypes = [C.c_void_p,C.c_double,C.c_double,C.c_bool,C.POINTER(C.c_bool),
                     C.POINTER(C.c_bool),C.POINTER(C.c_bool),C.POINTER(C.c_double)]
    free = library.fmi3FreeInstance
    free.argtypes = [C.c_void_p]
    free.restype = None
    instance = instantiate(b'probe',token,b'',False,False,False,False,None,0,None,None,None)
    assert instance
    try:
        assert enter(instance,False,0,0,False,0) == 0
        assert set_values(instance,(C.c_uint32*1)(refs['u']),1,(C.c_double*1)(1),1) == 0
        assert leave(instance) == 0
        for i in range(100):
            event,terminate,early,last = C.c_bool(),C.c_bool(),C.c_bool(),C.c_double()
            assert step(instance,i*.01,.01,True,C.byref(event),C.byref(terminate),C.byref(early),C.byref(last)) == 0
            assert not (event.value or terminate.value or early.value)
        value = (C.c_double*1)()
        assert get_values(instance,(C.c_uint32*1)(refs['x']),1,value,1) == 0
        assert value[0] == pytest.approx(1-math.exp(-1),abs=1e-8)
    finally:
        free(instance)
