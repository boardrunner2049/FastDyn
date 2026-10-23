// Independent probes for the remaining ConfiguredQuad export boundaries.
// Compile each model with --emit dae-json, then with --target fmi3.
within;
package FmiBuildBlockers
  record NumericProperties
    Real mass;
  end NumericProperties;

  record NamedProperties
    Real mass = 0;
    String componentId = "";
  end NamedProperties;

  function numericProperties
    input Real mass;
    output NumericProperties result;
  algorithm
    result.mass := mass;
  end numericProperties;

  function namedProperties
    input NamedProperties parts[:];
    output NamedProperties result;
  algorithm
    result.mass := 0;
    result.componentId := "";
    for i in 1:size(parts,1) loop
      assert(parts[i].componentId <> "invalid", "Invalid identifier");
      result.mass := result.mass + parts[i].mass;
    end for;
  end namedProperties;

  model NumericRecordCall
    parameter NumericProperties properties = numericProperties(1.5);
    Real x(start=0, fixed=true);
  equation
    der(x) = properties.mass;
  end NumericRecordCall;

  model StringRecordCall
    parameter NamedProperties parts[2](mass={1,2}, each componentId="");
    parameter NamedProperties properties = namedProperties(parts);
    Real x(start=0, fixed=true);
  equation
    der(x) = properties.mass;
  end StringRecordCall;

  model ContactEvent
    Real height(start=1, fixed=true);
    output Real force;
  equation
    der(height) = -1;
    force = if height <= 0 then -height else 0;
  end ContactEvent;

  model PublicBoolean
    Real height(start=1, fixed=true);
    output Boolean contact;
  equation
    der(height) = -1;
    contact = noEvent(height <= 0);
  end PublicBoolean;

  model CoupledInertia
    parameter Real inertia[2,2] = {{2,1},{1,2}};
    Real omega[2](each start=0, each fixed=true);
    Real alpha[2];
  equation
    inertia * alpha = {1,1};
    der(omega) = alpha;
  end CoupledInertia;
end FmiBuildBlockers;
