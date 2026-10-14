// Isolate String fields in pure-call inputs and outputs from assertion logic.
// Compare --emit dae-json, --emit solve-json, and --target fmi3.
within;
package StringPureCallInterface
  record NumericProperties
    Real mass;
  end NumericProperties;

  record NamedProperties
    Real mass;
    String componentId;
  end NamedProperties;

  function numericProperties
    input Real mass;
    output NumericProperties result;
  algorithm
    assert(mass >= 0, "Mass must be nonnegative");
    result.mass := mass;
  end numericProperties;

  function ignoreInputId
    input NamedProperties part;
    output NumericProperties result;
  algorithm
    result.mass := part.mass;
  end ignoreInputId;

  function produceOutputId
    input Real mass;
    output NamedProperties result;
  algorithm
    result.mass := mass;
    result.componentId := "";
  end produceOutputId;

  function copyMassWithoutAssertion
    input NamedProperties part;
    output NamedProperties result;
  algorithm
    result.mass := part.mass;
    result.componentId := "";
  end copyMassWithoutAssertion;

  model NumericWithAssertion
    parameter NumericProperties properties = numericProperties(1.5);
    Real x(start=0, fixed=true);
  equation
    der(x) = properties.mass;
  end NumericWithAssertion;

  model UnusedStringInput
    parameter NamedProperties part(mass=1.5, componentId="");
    parameter NumericProperties properties = ignoreInputId(part);
    Real x(start=0, fixed=true);
  equation
    der(x) = properties.mass;
  end UnusedStringInput;

  model ConstantStringOutput
    parameter NamedProperties properties = produceOutputId(1.5);
    Real x(start=0, fixed=true);
  equation
    der(x) = properties.mass;
  end ConstantStringOutput;

  model RuntimeStringOutput
    NamedProperties properties = produceOutputId(time + 1.5);
    Real x(start=0, fixed=true);
  equation
    der(x) = properties.mass;
  end RuntimeStringOutput;

  model RuntimeNumericWithAssertion
    NumericProperties properties = numericProperties(time + 1.5);
    Real x(start=0, fixed=true);
  equation
    der(x) = properties.mass;
  end RuntimeNumericWithAssertion;

  model EmptyIdWithoutAssertion
    parameter NamedProperties part(mass=1.5, componentId="");
    parameter NamedProperties properties = copyMassWithoutAssertion(part);
    Real x(start=0, fixed=true);
  equation
    der(x) = properties.mass;
  end EmptyIdWithoutAssertion;
end StringPureCallInterface;
