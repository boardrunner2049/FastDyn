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
@pytest.mark.parametrize('model,model_source,set_name,set_value,expected', [
    ('ContinuousProbe', 'model ContinuousProbe\ninput Real u(start=0);\noutput Real x(start=0,fixed=true);\nequation\nder(x)=u-x;\nend ContinuousProbe;\n', 'u', 1, 1-math.exp(-1)),
    ('ParameterFold', '''model ParameterFold
  function buildMatrix
    input Real scale;
    output Real values[2,2];
  algorithm
    for i in 1:2 loop
      for j in 1:2 loop
        values[i,j] := if scale > 0 then scale*(i+j) else 0;
      end for;
    end for;
    assert(values[1,1] >= 0, "negative entry");
  end buildMatrix;
  parameter Real scale = 2;
  parameter Real values[2,2] = buildMatrix(scale);
  parameter Real gain = values[1,1]+values[1,2]+values[2,1]+values[2,2];
  output Real x(start=0,fixed=true);
equation
  der(x) = gain;
end ParameterFold;
''', 'scale', 5, 60),
    ('NestedParameterFold', """model NestedParameterFold
  function accumulate
    input Real scale;
    output Real values[2,2];
  protected
    Real total;
  algorithm
    values := zeros(2,2);
    total := 0;
    for part in 1:2 loop
      total := total + scale;
      for i in 1:2 loop
        for j in 1:2 loop
          values[i,j] := values[i,j] + total*(i+j);
        end for;
      end for;
    end for;
    assert(values[1,1] >= 0, "negative accumulated entry");
  end accumulate;
  parameter Real scale = 2;
  parameter Real values[2,2] = accumulate(scale);
  parameter Real gain = values[1,1]+values[1,2]+values[2,1]+values[2,2];
  output Real x(start=0,fixed=true);
equation
  der(x) = gain;
end NestedParameterFold;
""", 'scale', 5, 180),
])
def test_source_fmu_packages_and_simulates(tmp_path, model, model_source, set_name, set_value, expected):
    source = tmp_path / (model + '.mo')
    source.write_text(model_source)
    repo = Path(__file__).resolve().parents[2]
    build = FmuBuild(name='probe', model=model, model_file=source,
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
        assert set_values(instance,(C.c_uint32*1)(refs[set_name]),1,(C.c_double*1)(set_value),1) == 0
        assert leave(instance) == 0
        for i in range(100):
            event,terminate,early,last = C.c_bool(),C.c_bool(),C.c_bool(),C.c_double()
            assert step(instance,i*.01,.01,True,C.byref(event),C.byref(terminate),C.byref(early),C.byref(last)) == 0
            assert not (event.value or terminate.value or early.value)
        value = (C.c_double*1)()
        assert get_values(instance,(C.c_uint32*1)(refs['x']),1,value,1) == 0
        assert value[0] == pytest.approx(expected,abs=1e-8)
    finally:
        free(instance)
