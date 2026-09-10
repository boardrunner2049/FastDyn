#!/usr/bin/env python3
"""Check the weight and moment actually evaluated by compiled payload FMUs."""
import argparse
import math
from pathlib import Path

from fmpy import read_model_description
from fmpy.fmi3 import FMU3Slave
from fastdyn.fmu_runtime import prepare


def check(path):
    model = read_model_description(path)
    runtime = prepare(path)
    fmu = FMU3Slave(guid=model.guid, unzipDirectory=str(runtime.resources.parent),
                   modelIdentifier=model.coSimulation.modelIdentifier, instanceName='payload_check')
    instantiated = False
    try:
        fmu.instantiate()
        instantiated = True
        fmu.enterInitializationMode(startTime=0)
        fmu.exitInitializationMode()
        variables = {v.name: v.valueReference for v in model.modelVariables}
        def values(name, count=1):
            return fmu.getFloat64([variables[name]], nValues=count)
        prefix = '' if 'payload_mass' in variables else 'plant.'
        mass = values(prefix + 'payload_mass')[0]
        force = values(prefix + 'force_b', 3)
        moment = values(prefix + 'moment_b', 3)
        attachment = values(prefix + 'attachment_b', 3)
        d = .11 / math.sqrt(2)
        for actual, expected in zip(attachment, [d, -d, 0.]):
            assert math.isclose(actual, expected, abs_tol=1e-12), attachment
        assert values('mass') == [.5], 'Payload changed the vehicle inertial mass'
        assert values('arm_length') == [.11], 'Payload changed the motor geometry'
        for actual, expected in zip(force, [0., 0., -9.8*mass]):
            assert math.isclose(actual, expected, abs_tol=1e-9), (force, mass)
        for actual, expected in zip(moment, [d*9.8*mass, d*9.8*mass, 0.]):
            assert math.isclose(actual, expected, abs_tol=1e-9), (moment, mass)
        if 'modulation' in variables:
            # Check the changed force equation at its mean, maximum and minimum,
            # independently of the controller's ability to reject the disturbance.
            period = values('period')[0]
            modulation = values('modulation')[0]
            current = 0.0
            for sample in (period / 4, period / 2, 3 * period / 4, period):
                while current < sample - 1e-12:
                    step = min(0.001, sample - current)
                    fmu.doStep(currentCommunicationPoint=current, communicationStepSize=step)
                    current += step
                actual = values('force_world', 3)
                expected = -9.8 * mass * (1 + modulation * math.sin(2 * math.pi * sample / period))
                assert math.isclose(actual[2], expected, abs_tol=1e-9), (sample, actual, expected)
                assert values('mass') == [.5], 'Varying force changed the inertial mass'
                body = values('force_b', 3)
                moment = values('moment_b', 3)
                cross = [-d * body[2], -d * body[2], d * (body[0] + body[1])]
                for measured, predicted in zip(moment, cross):
                    assert math.isclose(measured, predicted, abs_tol=1e-9), (moment, cross)
                print(f'  t={sample:g} s: world-down force {-actual[2]:.3f} N')
        fmu.terminate()
        print(f'{path}: payload {mass*1000:.1f} g, force {force}, moment {moment}')
    finally:
        if instantiated:
            fmu.freeInstance()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('fmu', type=Path, nargs='+')
    for path in parser.parse_args().fmu:
        check(path)
