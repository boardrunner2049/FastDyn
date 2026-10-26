package DimensionFixture
  record Geometry
    parameter Integer n=4;
  end Geometry;
  model Commands
    parameter Integer nChannels=4;
    input Real u[nChannels];
    output Real y[nChannels];
  equation
    y=u;
  end Commands;
  model Boundary
    parameter Geometry geometry;
    input Real pwm[geometry.n];
    Commands commands(nChannels=geometry.n);
  equation
    commands.u=pwm;
  end Boundary;
end DimensionFixture;

model RecordStructuralDimension
  extends DimensionFixture.Boundary(geometry=DimensionFixture.Geometry(n=6));
end RecordStructuralDimension;
