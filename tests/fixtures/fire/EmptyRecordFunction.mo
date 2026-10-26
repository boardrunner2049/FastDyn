model EmptyRecordFunction
  record Part
    parameter Real mass=1;
  end Part;
  function combine
    input Part parts[:];
    output Part result;
  algorithm
    result.mass := sum(parts.mass);
  end combine;
  model Assembly
    parameter Boolean assembled=false;
    parameter Integer n=0;
    parameter Part payloads[n];
    parameter Part aggregate;
    final parameter Part properties=if assembled then combine(payloads) else combine({aggregate});
    final parameter Real mass=properties.mass;
  end Assembly;
  parameter Integer n=0;
  parameter Part payloads[n];
  Assembly chassis(n=n,payloads=payloads);
  Real x(start=0,fixed=true);
equation
  der(x)=chassis.mass;
end EmptyRecordFunction;
