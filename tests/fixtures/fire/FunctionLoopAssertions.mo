// Compare DAE and Solve lowering of assertion dependencies without record inputs.
within;
package FunctionLoopAssertions
  function sharedLocal
    input Real values[:];
    output Real total;
  protected
    Real magnitude;
  algorithm
    total := 0;
    for i in 1:size(values, 1) loop
      magnitude := abs(values[i]);
      assert(magnitude < 10, "Value out of range");
      total := total + magnitude;
    end for;
  end sharedLocal;

  function singleUseLocal
    input Real values[:];
    output Real total;
  protected
    Real magnitude;
  algorithm
    total := 0;
    for i in 1:size(values, 1) loop
      magnitude := abs(values[i]);
      assert(magnitude < 10, "Value out of range");
      total := total + values[i];
    end for;
  end singleUseLocal;

  function inlinePredicate
    input Real values[:];
    output Real total;
  algorithm
    total := 0;
    for i in 1:size(values, 1) loop
      assert(abs(values[i]) < 10, "Value out of range");
      total := total + abs(values[i]);
    end for;
  end inlinePredicate;

  function outsideLoop
    input Real value;
    output Real total;
  protected
    Real magnitude;
  algorithm
    magnitude := abs(value);
    assert(magnitude < 10, "Value out of range");
    total := 2*magnitude;
  end outsideLoop;

  function loopCarried
    input Real values[:];
    output Real total;
  algorithm
    total := 0;
    for i in 1:size(values, 1) loop
      total := total + values[i];
      assert(total < 10, "Running total out of range");
    end for;
  end loopCarried;

  model SharedLocal
    Real x(start=0, fixed=true);
  equation
    der(x) = sharedLocal({time+1, time+2});
  end SharedLocal;

  model SingleUseLocal
    Real x(start=0, fixed=true);
  equation
    der(x) = singleUseLocal({time+1, time+2});
  end SingleUseLocal;

  model InlinePredicate
    Real x(start=0, fixed=true);
  equation
    der(x) = inlinePredicate({time+1, time+2});
  end InlinePredicate;

  model OutsideLoop
    Real x(start=0, fixed=true);
  equation
    der(x) = outsideLoop(time+1);
  end OutsideLoop;

  model LoopCarried
    Real x(start=0, fixed=true);
  equation
    der(x) = loopCarried({time+1, time+2});
  end LoopCarried;
end FunctionLoopAssertions;
