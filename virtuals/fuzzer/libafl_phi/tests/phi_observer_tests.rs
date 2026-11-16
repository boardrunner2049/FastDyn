use std::fs::{self, File};
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;

use banquo::operators::{Always, And};
use banquo::{predicate, Formula as BanquoFormula};
use banquo_parser::{parse_formula, ParsedFormula, Trace};

/// Helper to create a temporary working directory for tests and return to the
/// original directory afterwards.
fn with_temp_cwd<F: FnOnce(&PathBuf)>(test_fn: F) {
    let original_dir = std::env::current_dir().expect("get current dir");
    let temp_dir = original_dir.join("target").join("phi_observer_test_tmp");

    if temp_dir.exists() {
        fs::remove_dir_all(&temp_dir).expect("remove old temp dir");
    }
    fs::create_dir_all(&temp_dir).expect("create temp dir");

    std::env::set_current_dir(&temp_dir).expect("chdir temp");
    test_fn(&temp_dir);
    std::env::set_current_dir(&original_dir).expect("restore cwd");
}

#[test]
fn loads_formulas_from_text_file_and_evaluates() {
    with_temp_cwd(|_dir| {
        // 1) Create an STL formulas file in the temp working directory.
        let mut formulas_file = File::create("stl_formulas.txt").expect("create formulas file");
        // For now we only check that the file is readable. Parsing will fail
        // because the current parser syntax does not yet accept `and` between
        // inequalities; this test just proves that we can read the file
        // contents that `PhysicalObserver` will later feed into `parse_formula`.
        writeln!(formulas_file, "3.1*x <= 0.5*y").unwrap();

        // 2) Load and parse the formula using banquo-parser directly, just like
        //    `PhysicalObserver` does under the hood.
        let contents =
            fs::read_to_string("stl_formulas.txt").expect("read formulas file written by test");
        let line = contents.lines().next().unwrap().trim();
        assert_eq!(line, "3.1*x <= 0.5*y");

        // As a sanity check, round-trip this line through `parse_formula`.
        let formula: ParsedFormula = parse_formula(line).expect("parse simple STL-style formula");

        // 3) Build a trace that provides all variables used in the formula.
        let mut trace: Trace<std::collections::HashMap<String, f64>> = Trace::new();
        trace.insert(
            0.0,
            std::collections::HashMap::from([
                ("x".to_string(), 0.1_f64),
                ("y".to_string(), 1.0_f64),
            ]),
        );
        trace.insert(
            1.0,
            std::collections::HashMap::from([
                ("x".to_string(), 0.2_f64),
                ("y".to_string(), 2.0_f64),
            ]),
        );

        let metrics = formula.evaluate(&trace).expect("evaluate formula on trace");
        assert_eq!(metrics.len(), 2);
    });
}

#[test]
fn fails_when_variable_missing_from_trace() {
    // This test demonstrates the behaviour of Banquo/Banquo-parser when an STL
    // formula uses a variable that does not exist in the trace.
    //
    // The program (via `phi_observer`) currently unwraps the evaluation result,
    // so a missing variable would surface as a panic. Here we reproduce that
    // behaviour in a small, focused test.

    // Formula uses `pitch`, but the trace will only include `roll`.
    let formula: ParsedFormula =
        parse_formula("always 3.1*x <= 0.5*y and y <= 0.5").expect("parse well-formed formula");

    let mut trace: Trace<std::collections::HashMap<String, f64>> = Trace::new();
    // Intentionally omit `y` from the trace so evaluation fails with a
    // `Missing` variable error.
    trace.insert(
        0.0,
        std::collections::HashMap::from([("x".to_string(), 1.0_f64)]),
    );

    let result = formula.evaluate(&trace);
    assert!(result.is_err(), "expected evaluation to fail for missing variable");
}

#[test]
fn parses_production_stl_formulas_txt_with_same_logic() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/stl_formulas.txt");
    let formulas = load_stl_formulas(path);
    let bounds = [
        ("speed_ned_mps", 10.0),
        ("roll_rad", 0.3),
        ("pitch_rad", 0.3),
        ("accel_n_mps2", 100.0),
        ("accel_e_mps2", 100.0),
        ("accel_d_mps2", 100.0),
    ];
    for (signal, limit) in bounds {
        let selected: Vec<_> = formulas.iter()
            .filter(|(src, _)| src.split_whitespace().any(|token| token == signal))
            .collect();
        assert_eq!(selected.len(), 1, "Expected one envelope for {signal}");
        let (_, formula) = selected[0];
        for fraction in [-1.2_f64, -1.0, -0.5, 0.0, 0.5, 1.0, 1.2] {
            let value = limit * fraction;
            if signal == "speed_ned_mps" && value < 0.0 {
                continue;
            }
            let states = [0.0, value, 0.0].map(|x| {
                std::collections::HashMap::from([(signal.to_owned(), x)])
            });
            let [start, excursion, end] = states;
            let trace = Trace::from([(0.0, start), (1.0, excursion), (2.0, end)]);
            let rho = min_robustness(&formula.evaluate(&trace).unwrap());
            assert!((rho - (limit - value.abs())).abs() < 1e-9, "{signal} at {value}: {rho}");
        }
    }
}

#[test]
fn altitude_implication_preserves_constant_and_variable_signs() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/stl_formulas.txt");
    let (_, formula) = load_stl_formulas(path).into_iter()
        .find(|(src, _)| src.contains("implies") && src.contains("z <= 130.0"))
        .expect("existing altitude formula");
    for (time, altitude, expected) in [(0.0, 150.0, 1.0), (10.0, 120.0, 10.0),
                                       (10.0, 130.0, 0.0), (10.0, 150.0, -9.0)] {
        let state = std::collections::HashMap::from([
            ("sim_time".to_owned(), time), ("z".to_owned(), altitude),
        ]);
        let trace = Trace::from([(time, state)]);
        assert_eq!(min_robustness(&formula.evaluate(&trace).unwrap()), expected);
    }
}

/// Build a single state (one time step) with roll, pitch, roll_rate, lateral_accel.
fn state(
    roll: f64,
    pitch: f64,
    roll_rate: f64,
    lateral_accel: f64,
) -> std::collections::HashMap<String, f64> {
    std::collections::HashMap::from([
        ("roll".to_string(), roll),
        ("pitch".to_string(), pitch),
        ("roll_rate".to_string(), roll_rate),
        ("lateral_accel".to_string(), lateral_accel),
    ])
}

/// Compute STL robustness from metrics: minimum over the trace (same as PhysicalObserver).
fn min_robustness(metrics: &banquo_parser::Trace<f64>) -> f64 {
    metrics
        .iter()
        .fold(f64::INFINITY, |acc, (_t, v)| if *v < acc { *v } else { acc })
}

/// Load formulas from stl_formulas.txt using the same logic as PhysicalObserver.
fn load_stl_formulas(path: &str) -> Vec<(String, ParsedFormula)> {
    let file = File::open(path).unwrap_or_else(|e| panic!("open {}: {}", path, e));
    let reader = BufReader::new(file);
    let mut out = Vec::new();
    for (idx, line_res) in reader.lines().enumerate() {
        let line_num = idx + 1;
        let line = line_res.unwrap_or_else(|e| panic!("read line {}: {}", line_num, e));
        let s = line.trim().to_owned();
        if s.is_empty() || s.starts_with('#') {
            continue;
        }
        let parsed = parse_formula(&s).unwrap_or_else(|e| {
            panic!(
                "parse line {} of {}: \"{}\" -> {}",
                line_num, path, s, e
            );
        });
        out.push((s, parsed));
    }
    out
}

/// Reference formulas built the same way as before integrating the parser:
/// Always::unbounded(And::new(min_pred, max_pred)) for each variable's bounds.
fn reference_formula_roll() -> impl BanquoFormula<std::collections::HashMap<String, f64>, Metric = f64> {
    let roll_min_pred: banquo::Predicate = predicate! { -0.78539816339 <= roll };
    let roll_max_pred: banquo::Predicate = predicate! { roll <= 0.78539816339 };
    Always::unbounded(And::new(roll_min_pred, roll_max_pred))
}
fn reference_formula_pitch() -> impl BanquoFormula<std::collections::HashMap<String, f64>, Metric = f64> {
    let pitch_min_pred: banquo::Predicate = predicate! { -0.78539816339 <= pitch };
    let pitch_max_pred: banquo::Predicate = predicate! { pitch <= 0.78539816339 };
    Always::unbounded(And::new(pitch_min_pred, pitch_max_pred))
}
fn reference_formula_pre_fail_roll() -> impl BanquoFormula<std::collections::HashMap<String, f64>, Metric = f64> {
    let pre_fail_roll_min_pred: banquo::Predicate = predicate! { -0.3 <= roll };
    let pre_fail_roll_max_pred: banquo::Predicate = predicate! { roll <= 0.3 };
    Always::unbounded(And::new(pre_fail_roll_min_pred, pre_fail_roll_max_pred))
}
fn reference_formula_roll_growth() -> impl BanquoFormula<std::collections::HashMap<String, f64>, Metric = f64> {
    let roll_growth_min_pred: banquo::Predicate = predicate! { -0.5 <= roll_rate };
    let roll_growth_max_pred: banquo::Predicate = predicate! { roll_rate <= 0.5 };
    Always::unbounded(And::new(roll_growth_min_pred, roll_growth_max_pred))
}
fn reference_formula_lat_accel() -> impl BanquoFormula<std::collections::HashMap<String, f64>, Metric = f64> {
    let lat_accel_min_pred: banquo::Predicate = predicate! { -3.9 <= lateral_accel };
    let lat_accel_max_pred: banquo::Predicate = predicate! { lateral_accel <= 3.9 };
    Always::unbounded(And::new(lat_accel_min_pred, lat_accel_max_pred))
}

#[test]
fn stl_formulas_parse_and_evaluate_accurately() {
    // Keep the historical parser-vs-macro fixtures independent of the active mission policy.
    let parsed: Vec<_> = [
        "always -0.78539816339 <= roll and roll <= 0.78539816339",
        "always -0.78539816339 <= pitch and pitch <= 0.78539816339",
        "always -0.3 <= roll and roll <= 0.3",
        "always -0.5 <= roll_rate and roll_rate <= 0.5",
        "always -3.9 <= lateral_accel and lateral_accel <= 3.9",
    ].into_iter().map(|src| (src.to_owned(), parse_formula(src).unwrap())).collect();

    const TOL: f64 = 1e-9;
    type State = std::collections::HashMap<String, f64>;

    // All variables within every formula's bounds (tightest: roll ±0.3, roll_rate ±0.5).
    let trace_in_bounds: Trace<State> = Trace::from([
        (0.0, state(0.0, 0.0, 0.0, 0.0)),
        (1.0, state(0.2, 0.3, 0.2, 1.0)),
    ]);

    fn ref_robustness(idx: usize, t: &Trace<State>) -> f64 {
        match idx {
            0 => min_robustness(&reference_formula_roll().evaluate(t).unwrap_or_else(|_| panic!("reference 0"))),
            1 => min_robustness(&reference_formula_pitch().evaluate(t).unwrap_or_else(|_| panic!("reference 1"))),
            2 => min_robustness(&reference_formula_pre_fail_roll().evaluate(t).unwrap_or_else(|_| panic!("reference 2"))),
            3 => min_robustness(&reference_formula_roll_growth().evaluate(t).unwrap_or_else(|_| panic!("reference 3"))),
            4 => min_robustness(&reference_formula_lat_accel().evaluate(t).unwrap_or_else(|_| panic!("reference 4"))),
            _ => panic!("formula index out of range"),
        }
    }

    for (idx, (src, p)) in parsed.iter().enumerate() {
        let ref_r = ref_robustness(idx, &trace_in_bounds);
        let parsed_r = min_robustness(&p.evaluate(&trace_in_bounds).expect("parsed eval"));

        assert!(
            (ref_r - parsed_r).abs() < TOL,
            "formula {}: parsed should match reference on in-bounds trace; reference={}, parsed={}, source={:?}",
            idx, ref_r, parsed_r, src
        );
        assert!(
            ref_r >= 0.0,
            "formula {}: in-bounds trace should have non-negative robustness (no violation); got {}",
            idx, ref_r
        );
    }

    let violating_traces: [Trace<State>; 5] = [
        Trace::from([(0.0, state(1.0, 0.0, 0.0, 0.0))]),
        Trace::from([(0.0, state(0.0, 1.0, 0.0, 0.0))]),
        Trace::from([(0.0, state(0.5, 0.0, 0.0, 0.0))]),
        Trace::from([(0.0, state(0.0, 0.0, 1.0, 0.0))]),
        Trace::from([(0.0, state(0.0, 0.0, 0.0, 5.0))]),
    ];

    for (idx, (src, p)) in parsed.iter().enumerate() {
        let trace = &violating_traces[idx];
        let ref_r = ref_robustness(idx, trace);
        let parsed_r = min_robustness(&p.evaluate(trace).expect("parsed eval"));

        assert!(
            (ref_r - parsed_r).abs() < TOL,
            "formula {}: parsed should match reference on violating trace; reference={}, parsed={}, source={:?}",
            idx, ref_r, parsed_r, src
        );
        assert!(
            ref_r < 0.0,
            "formula {}: violating trace should have negative robustness; got {}",
            idx, ref_r
        );
    }

    for (src, p) in &parsed {
        assert_eq!(
            p.debug_source().unwrap(),
            src.as_str(),
            "parsed formula debug_source should equal formula string"
        );
    }
}


