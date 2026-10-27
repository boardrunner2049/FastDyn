use libafl::executors::ExitKind;
use libafl::observers::Observer;
use libafl::state::HasExecutions;
use libafl::Error;
use libafl_bolts::Named;

use core::f64;
use csv::Reader;
use serde::{Deserialize, Serialize};
use std::borrow::Cow;
use std::collections::HashMap;
use std::env;
use std::fs::{create_dir_all, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Child, Command};
use std::thread::sleep;
use std::time::Duration;

// use banquo_parser::{parse_formula, Formula, ParsedFormula, Trace};
use banquo_parser::{parse_formula, ParsedFormula};

// use banquo::{
//     Trace
//     predicate,
//     operators::{Always, Eventually, Implies, And, Or, Not},
//     evaluate,
// };

use banquo::operators::{Always, And, Eventually, Implies, Not, Or};
use banquo::predicate::FormulaError;
use banquo::{evaluate, predicate, EvaluationError, Formula, Predicate, Trace};

use crate::cpexp_state::{HasLatestRobustness, HasOptimizer};
use crate::trace_parser::parse_trace_log;

/// Path to the text file containing STL-style formulas, one per line.
/// Lines starting with `#` and blank lines are ignored.
const STL_FORMULAS_PATH: &str = "stl_formulas.txt";

fn optifuzz_backend_is_fmuv3() -> bool {
    env::var("FASTDYN_OPTIFUZZ_BACKEND")
        .unwrap_or_else(|_| "fmuv3".to_string())
        .eq_ignore_ascii_case("fmuv3")
}

#[derive(Deserialize)]
struct State {
    // clock
    time: f64,

    // pose
    x: f64,
    y: f64,
    z: f64,
    roll: f64,
    pitch: f64,
    yaw: f64,

    // navsat
    lin_velo_x: f64,
    lin_velo_y: f64,
    lin_velo_z: f64,

    // imu
    ang_velo_x: f64,
    ang_velo_y: f64,
    ang_velo_z: f64,
    lin_accel_x: f64,
    lin_accel_y: f64,
    lin_accel_z: f64,
}

#[derive(Deserialize)]
struct ContestState {
    time: f64,
    lyapunov: f64,
}

/**
 * A helper function to create the OptiFuzz default trace state.
 */
fn create_state(
    sim_time: f64,
    x: f64,
    y: f64,
    z: f64,
    roll: f64,
    pitch: f64,
    yaw: f64,
    lin_velo_x: f64,
    lin_velo_y: f64,
    lin_velo_z: f64,
    ang_velo_x: f64,
    ang_velo_y: f64,
    ang_velo_z: f64,
    lin_accel_x: f64,
    lin_accel_y: f64,
    lin_accel_z: f64,
) -> HashMap<String, f64> {
    HashMap::from([
        ("time".to_string(), sim_time),
        ("x".to_string(), x),
        ("y".to_string(), y),
        ("z".to_string(), z),
        ("roll".to_string(), roll),
        ("pitch".to_string(), pitch),
        ("yaw".to_string(), yaw),
        ("lin_velo_x".to_string(), lin_velo_x),
        ("lin_velo_y".to_string(), lin_velo_y),
        ("lin_velo_z".to_string(), lin_velo_z),
        ("ang_velo_x".to_string(), ang_velo_x),
        ("ang_velo_y".to_string(), ang_velo_y),
        ("ang_velo_z".to_string(), ang_velo_z),
        ("lin_accel_x".to_string(), lin_accel_x),
        ("lin_accel_y".to_string(), lin_accel_y),
        ("lin_accel_z".to_string(), lin_accel_z),
    ])
}

fn create_default_trace(mut csv_reader: csv::Reader<std::fs::File>) -> Trace<HashMap<String, f64>> {
    let mut trace: Trace<HashMap<String, f64>> = Trace::new();
    for result in csv_reader.deserialize() {
        let state: State = result.unwrap();
        trace.insert(
            state.time,
            create_state(
                state.time,
                state.x,
                state.y,
                state.z,
                state.roll,
                state.pitch,
                state.yaw,
                state.lin_velo_x,
                state.lin_velo_y,
                state.lin_velo_z,
                state.ang_velo_x,
                state.ang_velo_y,
                state.ang_velo_z,
                state.lin_accel_x,
                state.lin_accel_y,
                state.lin_accel_z,
            ),
        );
    }

    trace
}

/**
 * A helper function to create the ConTest trace state.
 */
fn create_contest_state(sim_time: f64, lyapunov: f64) -> HashMap<String, f64> {
    HashMap::from([
        ("time".to_string(), sim_time),
        ("lyapunov".to_string(), lyapunov),
    ])
}

fn create_contest_trace(mut csv_reader: csv::Reader<std::fs::File>) -> Trace<HashMap<String, f64>> {
    let mut trace: Trace<HashMap<String, f64>> = Trace::new();
    for result in csv_reader.deserialize() {
        let record: ContestState = result.unwrap();
        let sim_time: f64 = record.time;
        let lyapunov: f64 = record.lyapunov;
        trace.insert(sim_time, create_contest_state(sim_time, lyapunov));
    }

    trace
}

fn create_contest_trace2(mut csv_reader: csv::Reader<std::fs::File>) -> Trace<HashMap<String, f64>> {
    let headers = csv_reader.headers().cloned().unwrap();
    let mut trace: Trace<HashMap<String, f64>> = Trace::new();
    for result in csv_reader.records()
    {
        let record = result.unwrap();
        let mut row_map = HashMap::new();
        let mut timestamp: Option<f64> = None; 

        for i in 0..record.len()
        {
            let header_name = &headers[i];
            let cell_value_str = &record[i];
            if let Ok(value) = cell_value_str.parse::<f64>() {
                row_map.insert(header_name.to_string(), value);
                if i == 0 {
                    timestamp = Some(value);
                }
            }
        }
        if timestamp.is_some() {
            trace.insert(timestamp.unwrap(), row_map);
        }
        else{
            panic!("Error: Failed to parse a valid timestamp for index 0 in this row.");
        }
    }
    trace
}

#[derive(Serialize, Deserialize)]
pub struct PhysicalObserver {
    name: Cow<'static, str>,
    trace_time_step: f64,            // Time step between recorded trace points
    sim_time_limit: f64,             // Maximum simulation time to record
    trace_log_dir: String,           // Directory to store the trace logs
    robustness_log_dir: String,      // Directory to store the robustness logs
    latest_robustness_vec: Vec<f64>, // Vector to store latest robustness values for each STL formula

    /// Parsed STL-style formulas loaded from `stl_formulas.txt` at startup.
    #[serde(skip)]
    formulas: Vec<ParsedFormula>,

    // The indices of the formulas to be optimized in this instantiation.
    // We observe all formulas, but optimize a subset.
    #[serde(skip)]
    optimized_formula_indices: Vec<usize>,

    #[serde(skip)]
    recorder_process: Option<Child>, // Child process handle for the recorder
}

impl Named for PhysicalObserver {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl PhysicalObserver {
    pub fn new(step: f64, limit: f64, trace_log_dir: &str, robustness_log_dir: &str) -> Self {
        let step: f64 = if step <= 0.0 { 0.1 } else { step };
        let limit: f64 = if limit <= 0.0 { 10.0 } else { limit };

        // Load STL formulas from the configured text file.
        let (formulas, mut opt_indices) = Self::load_formulas_from_file(STL_FORMULAS_PATH);

        // Initialize robustness vector with +inf for each loaded formula so that
        // the first evaluation will always improve it.
        let robustness_vec: Vec<f64> = if formulas.is_empty() {
            // Fail fast with a clear message if no formulas were loaded.
            panic!(
                "No STL formulas loaded from '{}'. \
                 Please create this file with one STL-style formula per line.",
                STL_FORMULAS_PATH
            );
        } else {
            vec![f64::INFINITY; formulas.len()]
        };

        Self {
            name: Cow::from("PhysicalObserver"),
            trace_time_step: step,
            sim_time_limit: limit,
            trace_log_dir: String::from(trace_log_dir),
            robustness_log_dir: String::from(robustness_log_dir),
            latest_robustness_vec: robustness_vec,
            formulas,
            optimized_formula_indices: opt_indices,
            recorder_process: None,
        }
    }

    pub fn get_robustness_vec(&self) -> &Vec<f64> {
        &self.latest_robustness_vec
    }

    /// Load STL-style formulas from a plain text file using `banquo-parser`.
    ///
    /// - One formula per non-empty line.
    /// - Lines starting with `#` are treated as comments and ignored.
    fn load_formulas_from_file(path: &str) -> (Vec<ParsedFormula>, Vec<usize>) {
        let file = File::open(path).unwrap_or_else(|err| {
            panic!(
                "Failed to open STL formula file '{}': {}\n\
                 Create this file and add one formula per line, \
                 e.g.:\n  always -0.78539816339 <= roll and roll <= 0.78539816339",
                path, err
            )
        });

        let reader = BufReader::new(file);
        let mut formulas = Vec::new();
        let mut opt_indices: Vec<usize> = Vec::new();

        for (idx, line_res) in reader.lines().enumerate() {
            let line_num = idx + 1;
            let line = line_res.unwrap_or_else(|err| {
                panic!("Error reading line {} from '{}': {}", line_num, path, err)
            });

            // Work with an owned string for the formula to avoid lifetime
            // issues tied to the `line` binding.
            let formula_str = line.trim().to_owned();

            // Skip blank lines.
            if formula_str.is_empty() {
                continue;
            }

            if formula_str.starts_with('#') {
                // Check for optimization flag in comment lines, e.g.:
                // # OPTIMIZE
                if formula_str == "# OPTIMIZE" {
                    opt_indices.push(formulas.len());
                }

                continue;
            }

            let parsed = parse_formula(&formula_str).unwrap_or_else(|e| {
                panic!(
                    "Failed to parse STL formula on line {} of '{}': \"{}\"\nError: {}",
                    line_num, path, formula_str, e
                );
            });

            formulas.push(parsed);
        }

        // No explicit # OPTIMIZE --> optimize all formulas by default
        if opt_indices.is_empty() {
            for i in 0..formulas.len() {
                opt_indices.push(i);
            }
        }

        (formulas, opt_indices)
    }
}

impl<I, S> Observer<I, S> for PhysicalObserver
where
    S: HasExecutions + HasOptimizer + HasLatestRobustness,
{
    fn pre_exec(&mut self, state: &mut S, _input: &I) -> Result<(), Error> {
        if optifuzz_backend_is_fmuv3() {
            return Ok(());
        }

        if self.recorder_process.is_some() {
            // The recorder process should be none...
            panic!("Error: Recorder process is NOT none in pre_exec!");
        }

        // Get the path of ./trace_recorder no matter where you run ./baby_fuzzer
        let mut recorder_path =
            std::env::current_exe().expect("Failed to get current executable path");
        recorder_path.pop(); // remove baby_fuzzer filename
        recorder_path.push("trace_recorder");

        let spawn_result = Command::new(recorder_path)
            .args([
                (state.executions() + 1).to_string(),
                self.trace_time_step.to_string(),
                self.sim_time_limit.to_string(),
                self.trace_log_dir.clone(),
            ])
            .spawn();

        if spawn_result.is_err() {
            panic!(
                "Error: Failed to start trace_recorder process: {}",
                spawn_result.err().unwrap()
            );
        } else {
            self.recorder_process = Some(spawn_result.unwrap());
        }

        Ok(())
    }

    fn post_exec(&mut self, state: &mut S, _input: &I, _exit_kind: &ExitKind) -> Result<(), Error> {
        if optifuzz_backend_is_fmuv3() {
            // parse the trace log generated for this (get max, min, avg values?)
            // compare max and min againt banquo STL formulas
            // return robustness
            
            let trace_dir = Path::new(&self.trace_log_dir);
            let trace_log_path = format!(
                "{}/trace_{}.csv",
                self.trace_log_dir,
                state.executions()
            );
            //let trace_stats = parse_trace_log(&trace_log_path).unwrap();

            let csv_reader_status = csv::ReaderBuilder::new()
                .has_headers(true)
                .from_path(&trace_log_path);

            if csv_reader_status.is_err() {
                println!(
                    "Warning: Failed to open trace log file for parsing {}",
                    state.executions()
                );
                // TODO: should return an error or something???  - should always be able to open
                println!("Skipping physical observation...");
                return Ok(());
            }

            let mut csv_reader = csv_reader_status.unwrap();
            //let headers = csv_reader.headers()?.clone();
            // TODO: using a Vector here instead of BTreeMap is more efficient when inserting (since we know dataset is chronological)
            let mut trace: Trace<HashMap<String, f64>> = create_contest_trace2(csv_reader);
            self.latest_robustness_vec.clear();

            // trace is created, now I need to feed into Banquo to eval STL formulas
            // should get robustness for each formula
            //self.latest_robustness_vec
            //    .push(evaluate(&trace, &contest_formula).unwrap());

            
            let log_dir = Path::new(&self.robustness_log_dir);
            create_dir_all(log_dir).expect("Unable to create robustness log directory");
            let robustness_log_path = format!(
                "{}/robustness_{}.csv",
                self.robustness_log_dir,
                state.executions()
            );
            let mut file = OpenOptions::new()
                .write(true)
                .truncate(true)
                .create(true)
                .open(&robustness_log_path)
                .expect("Unable to create robustness log file");

            for (idx, formula) in self.formulas.iter().enumerate() {
                let metrics = formula
                    .evaluate(&trace)
                    .unwrap_or_else(|e| {
                        panic!(
                            "Error evaluating STL formula #{} from '{}': {}",
                            idx + 1,
                            STL_FORMULAS_PATH,
                            e
                        )
                    });

                // Reduce the per-time-step metrics to a single robustness value by
                // taking the minimum over the trace, matching the classic STL
                // robustness semantics.
                let robustness = metrics
                    .iter()
                    .fold(f64::INFINITY, |acc, (t, v)| {
                        let _ = t; // ignore time, only robustness value matters
                        if *v < acc { *v } else { acc }
                    });
                self.latest_robustness_vec.push(robustness);

                writeln!(file, "Formula{}={}, robustness={},", idx, formula.debug_source().unwrap(), robustness).unwrap(); 
            }

            // Get minimum robustness value of the OPTIMIZED formulas, and put it in the state to tell the optimizer
            let mut min_optimized_robustness: f64 = f64::INFINITY;
            for opt in self.optimized_formula_indices.iter() {
                let current_robustness = self.latest_robustness_vec[*opt];
                if current_robustness < min_optimized_robustness {
                    min_optimized_robustness = current_robustness;
                }
            }
            writeln!(file, "min robustness={}", min_optimized_robustness).unwrap(); 
            state.set_latest_robustness(min_optimized_robustness);

            return Ok(());
        }



        if self.recorder_process.is_none() {
            // The recorder process should NOT be none...
            panic!("Error: Recorder process is none in post_exec!");
        }

        // Before reading the trace log, ensure the recorder process has finished
        let wait = self.recorder_process.as_mut().unwrap().wait().unwrap();
        if !wait.success() {
            println!("Warning: Recorder process finished with error...");
        }
        self.recorder_process = None;

        let newest_trace_log_path =
            format!("{}/trace_{}.csv", self.trace_log_dir, state.executions());

        // let csv_reader_status = csv::ReaderBuilder::new()
        //     .has_headers(false)
        //     .from_path(&newest_trace_log_path);

        // if csv_reader_status.is_err() {
        //     println!("Warning: Failed to open trace log file for execution {}", state.executions());
        //     println!("Skipping physical observation...");
        //     return Ok(());
        // }

        // let mut csv_reader = csv_reader_status.unwrap();
        // let mut trace: Trace<HashMap<String, f64>> = create_default_trace(csv_reader);

        let newest_lyapunov_log_path =
            format!("{}/lyapunov_{}.csv", self.trace_log_dir, state.executions());
        let initial_distance_to_waypoint: f64 = 1000.0;
        let epsilon: f64 = 100.0; // WP_RADIUS
        let mission_timeout: f64 = 180.0;

        let lyapunov_process = Command::new("python3")
            .arg("calculate_lyapunov.py")
            .arg(newest_trace_log_path)
            .arg(&newest_lyapunov_log_path)
            .arg(initial_distance_to_waypoint.to_string())
            .arg(epsilon.to_string())
            .arg(mission_timeout.to_string())
            .spawn();
        if lyapunov_process.is_err() {
            panic!("Error: Failed to start Lyapunov calculation script!");
        }
        let _ = lyapunov_process.unwrap().wait();

        let csv_reader_status = csv::ReaderBuilder::new()
            .has_headers(false)
            .from_path(&newest_lyapunov_log_path);

        if csv_reader_status.is_err() {
            println!(
                "Warning: Failed to open lyapunov log file for execution {}",
                state.executions()
            );
            println!("Skipping physical observation...");
            return Ok(());
        }

        let mut csv_reader = csv_reader_status.unwrap();
        let mut trace: Trace<HashMap<String, f64>> = create_contest_trace(csv_reader);

        self.latest_robustness_vec.clear();

        // ------------------------------------------------------------------------------------------------
        // ------------------------------------- ONLY MODIFY BELOW ----------------------------------------
        // ------------------------------------------------------------------------------------------------

        /**
         * DEFINING STL FORMULAS
         *
         * Signal-Temporal Logic (STL) formulas are the foundation of CP-Explore's physical observations.
         * They guide the joint fuzzer and optimizer's search for compromising inputs.
         * By Resolute Hunter, users will be able to specify STL forrmulas in English in "stl_formulas.txt".
         * For now, the user must manually create formulas.
         *
         * The example formlua below was used in the ArduPlane fuzzing campaign to monitor for altitude instability.
         * For more information about creating STL formulas, see the Banquo crate documentation at the link below:
         *
         * https://docs.rs/banquo/latest/banquo/
         */
        // First, you must define a list of predicates.
        // These are inequalities that relate metrics from the CPS's physical state to arbitrary float expressions.
        // See the State struct (near line 40) for the available metrics you can use in predicates.
        // The State struct only contains a subset of the available metrics for now. We will complete the list later.
        // let min_z_pred = predicate!{ 50.0 <= z };
        // let time_pred = predicate!{ 110.0 <= time };
        let exponential_stability_pred = predicate! { lyapunov <= 0.0 };

        // After creating the predicates, use the logical operators to form a complete STL formula.
        // "For all simulation times, if the simulation time is greater than 110, then the plane's Z coordinate must
        // be greater than 50."
        // let min_z_formula: Always<Implies<Predicate, Predicate>> = Always::unbounded(Implies::new(time_pred.clone(), min_z_pred));
        let contest_formula = Always::unbounded(exponential_stability_pred);

        // After creating all your formulas, make sure to push their evaluations into the robustness vector, like this:
        // self.latest_robustness_vec.push(evaluate(&trace, &min_z_formula).unwrap());
        self.latest_robustness_vec
            .push(evaluate(&trace, &contest_formula).unwrap());

        // ------------------------------------------------------------------------------------------------
        // ------------------------------------- ONLY MODIFY ABOVE ----------------------------------------
        // ------------------------------------------------------------------------------------------------

        // Search for the minimum robustness out of all the STL formulas for this run
        let mut min_robustness: f64 = f64::INFINITY;
        for robustness in self.latest_robustness_vec.iter() {
            if *robustness < min_robustness {
                min_robustness = *robustness;
            }
        }
        state.set_latest_robustness(min_robustness);

        // for (idx, formula) in self.formulas.iter().enumerate() {
        //     let metrics = formula
        //         .evaluate(&trace)
        //         .unwrap_or_else(|e| {
        //             panic!(
        //                 "Error evaluating STL formula #{} from '{}': {}",
        //                 idx + 1,
        //                 STL_FORMULAS_PATH,
        //                 e
        //             )
        //         });

        //     // Reduce the per-time-step metrics to a single robustness value by
        //     // taking the minimum over the trace, matching the classic STL
        //     // robustness semantics.
        //     let robustness = metrics
        //         .iter()
        //         .fold(f64::INFINITY, |acc, (t, v)| {
        //             let _ = t; // ignore time, only robustness value matters
        //             if *v < acc { *v } else { acc }
        //         });

        //     self.latest_robustness_vec.push(robustness);
        // }

        // println!("Robustness for execution {}: {:?}", state.executions(), self.latest_robustness_vec);

        // Get minimum robustness value of the OPTIMIZED formulas, and put it in the state to tell the optimizer
        // let mut min_optimized_robustness: f64 = f64::INFINITY;
        // for opt in self.optimized_formula_indices.iter() {
        //     let current_robustness = self.latest_robustness_vec[*opt];
        //     if current_robustness < min_optimized_robustness {
        //         min_optimized_robustness = current_robustness;
        //     }
        // }
        // state.set_latest_robustness(min_optimized_robustness);

        // Also, log the robustness values to a file for later analysis
        let robustness_log_path = format!(
            "{}/robustness_{}.csv",
            self.robustness_log_dir,
            state.executions()
        );
        let log_dir = Path::new(&self.robustness_log_dir);
        create_dir_all(log_dir).expect("Unable to create robustness log directory");

        let mut file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&robustness_log_path)
            .expect("Unable to create robustness log file");

        writeln!(file, "Formula,Robustness").unwrap();
        for (i, robustness) in self.latest_robustness_vec.iter().enumerate() {
            writeln!(file, "Formula_{},{}", i, robustness).unwrap();
        }
        file.flush().unwrap();

        Ok(())
    }
}
