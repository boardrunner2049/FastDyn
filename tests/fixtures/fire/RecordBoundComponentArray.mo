// Reproduces the ConfiguredQuad rotor-member failure with Rumoca 81b489b8.
// No FIRE or Modelica Standard Library dependency is needed.
//
// From the FastDyn repository root:
// third_party/common/rumoca/target/debug/rumoca compile \
//   tests/fixtures/fire/RecordBoundComponentArray.mo \
//   --model RecordBoundComponentArray.ViaRecordBinding --emit dae-json
//
// Expected: all three models compile and plant.y = {time+1, time+2, time+3}.
// Before the compiler fix: ViaRecordBinding fails with ED019 at rotors[i].demand;
//           ViaFieldModifiers expands only two rotors and also fails;
//           ViaSourceFieldModifiers compiles with three rotors.
// Use --emit flat-json to compare the retained component identities.
// The nondefault extent (3 rather than 2) detects stale record defaults.
within;
package RecordBoundComponentArray
  record Geometry
    parameter Integer nRotors = 2;
  end Geometry;

  model Rotor
    input Real demand;
    output Real y;
  equation
    y = demand;
  end Rotor;

  model Plant
    parameter Geometry geometry;
    Rotor rotors[geometry.nRotors];
    output Real y[geometry.nRotors];
  equation
    for i in 1:geometry.nRotors loop
      rotors[i].demand = time + i;
      y[i] = rotors[i].y;
    end for;
  end Plant;

  model ViaRecordBinding
    parameter Geometry geometry = Geometry(nRotors = 3);
    Plant plant(geometry = geometry);
  end ViaRecordBinding;

  model ViaFieldModifiers
    parameter Geometry geometry = Geometry(nRotors = 3);
    Plant plant(geometry(nRotors = geometry.nRotors));
  end ViaFieldModifiers;

  // A direct field modifier on the source record is another passing control.
  model ViaSourceFieldModifiers
    parameter Geometry geometry(nRotors = 3);
    Plant plant(geometry = geometry);
  end ViaSourceFieldModifiers;
end RecordBoundComponentArray;
