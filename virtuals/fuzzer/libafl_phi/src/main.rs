// -----------------------------------------------------------------------------------------------
// ----------------------------------- DO NOT MODIFY BELOW ---------------------------------------
// -----------------------------------------------------------------------------------------------

mod cpexp_input;
mod cpexp_state;
mod lambda_mutational;
mod mission_execution;
mod param_shim;
mod phi_feedback;
mod phi_objective;
mod phi_observer;
mod phi_stage;

use std::env;
#[cfg(windows)]
use std::ptr::write_volatile;
use std::{
    path::{self, PathBuf},
    process::exit,
    ptr::write,
    thread::sleep,
    time::Duration,
};

use env_logger::Target;
#[cfg(feature = "tui")]
use libafl::monitors::tui::TuiMonitor;
#[cfg(not(feature = "tui"))]
use libafl::monitors::SimpleMonitor;
use libafl::{
    corpus::{InMemoryCorpus, OnDiskCorpus},
    events::SimpleEventManager,
    executors::{CommandExecutor, ExitKind, InProcessExecutor},
    feedback_or,
    feedbacks::{CrashFeedback, MaxMapFeedback, TimeoutFeedback},
    fuzzer::{Fuzzer, StdFuzzer},
    generators::RandPrintablesGenerator,
    inputs::{BytesInput, HasTargetBytes},
    mutators::{havoc_mutations::havoc_mutations, scheduled::HavocScheduledMutator},
    observers::StdMapObserver,
    schedulers::QueueScheduler,
    stages::{mutational::StdMutationalStage, AflStatsStage, CalibrationStage},
    state::StdState,
};
use libafl_bolts::{
    current_nanos, nonnull_raw_mut, nonzero, rands::StdRand, tuples::tuple_list, AsSlice,
};

use std::process::{Child, Command, Stdio};

use scirs2_core::ndarray::Array1;
use scirs2_optimize::global::{BayesianOptimizationOptions, BayesianOptimizer, Space};
use scirs2_optimize::prelude::Parameter;

use cpexp_input::{CPExpInput, EnvInput, HasEnvConfig, HasParamBytes, ParamInput, TargetInput};
use cpexp_state::{CPExpState, HasInputLibrary};
use lambda_mutational::LambdaMutationalStage;
use phi_feedback::PhysicalFeedback;
use phi_objective::PhysicalObjective;
use phi_observer::PhysicalObserver;
use phi_stage::PhiStage;

use mission_execution::execute_mission;

use libafl::executors::{Executor, WithObservers};
use libafl::state::{HasCorpus, HasExecutions};
use std::marker::PhantomData;

/**
 * Given a CPExpInput, automatically create a search space for the optimizer.
 */
fn search_space_from_input_library(input_lib: &CPExpInput) -> Space {
    let mut space = Space::new();

    // Add parameter inputs to space
    let param_info_vec = input_lib.get_param_input();
    for param in param_info_vec.iter() {
        let param_name = param.get_name();
        let param_range = param.get_range();
        space = space.add(param_name, Parameter::Real(param_range.0, param_range.1));
    }

    // Add environmental inputs to space
    let env_info_vec = input_lib.get_env_input();
    for env in env_info_vec.iter() {
        let env_name = env.get_name();
        let env_range = env.get_range();
        space = space.add(env_name, Parameter::Real(env_range.0, env_range.1));
    }

    space
}

struct FastDynExecutor<S> {
    phantom: PhantomData<S>,
    // fuzz_buffer: *mut FuzzBuffer,
}

impl<S> FastDynExecutor<S> {
    // pub fn new(_state: &S, fuzz_buffer: *mut FuzzBuffer) -> Self {
    pub fn new(_state: &S) -> Self {
        Self {
            phantom: PhantomData,
            // fuzz_buffer,
        }
    }
}

impl<EM, S, Z> Executor<EM, TargetInput, S, Z> for FastDynExecutor<S>
where
    S: HasCorpus<TargetInput> + HasExecutions + HasInputLibrary,
    // I: TargetInput// HasParamBytes + HasEnvConfig,
{
    fn run_target(
        &mut self,
        _fuzzer: &mut Z,
        state: &mut S,
        _mgr: &mut EM,
        input: &TargetInput,
    ) -> Result<ExitKind, libafl::Error> {
        let executions = state.executions_mut();
        *executions += 1;
        env::set_var("EXECUTION", executions.to_string());

        let param_info = state.input_library().get_param_input().clone();
        let mut param_names = String::new();
        for param in param_info.iter() {
            param_names.push_str(&param.get_name());
            param_names.push(',');
        }
        let vehicle = optifuzz_vehicle();
        let mission_file = optifuzz_mission_file();

        let result: ExitKind = execute_mission(
            input,
            param_names,
            &vehicle,
            &mission_file,
            MISSION_TIMEOUT,
            POST_ARM_PARAMETER_UPDATE_DELAY,
            NOISE_APPLICATION_SIM_TIME,
            HEADLESS_MODE,
        );

        Ok(result)
    }
}

// Coverage map shared with the instrumentation in core.c
const MAP_SIZE: usize = 65536; // same as AFL, make sure the definition of this size in C is the same
#[no_mangle]
pub static mut CVG: [u8; MAP_SIZE] = [0; MAP_SIZE];

const TRACE_LOG_PATH: &str = "./trace_logs";
const ROBUSTNESS_LOG_PATH: &str = "./robustness_logs";
const CRASH_LOG_PATH: &str = "./crashes";
const CORPUS_LOG_PATH: &str = "./corpus";

// ------------------------------------------------------------------------------------------------
// ----------------------------------- DO NOT MODIFY ABOVE ----------------------------------------
// ------------------------------------------------------------------------------------------------

/**
 * MISCELLANEOUS CONSTANT VALUES
 *
 * Edit these values as needed.
 *
 * SYSTEM_UNDER_TEST: The name of the CPS to test.
 * MISSION_WAYPOINT_FILE_NAME: The name of the file containing mission waypoints for the system under test.
 *     For the default FMUv3 backend, this may be a repo-relative path.
 * MISSION_TIMEOUT: The maximum simulation time allowed before it is killed
 * POST_ARM_PARAMETER_UPDATE_DELAY: The time to wait after arming the CPS before updating parameter values.
 * NOISE_APPLICATION_SIM_TIME: The simulation time at which noise should be applied to the system's sensors.
 * RECORDING_TIMESTEP: An approximation of how often the CPS's physical state is logged during a simulation.
 *     NOTE: We do not recommend setting this value less than 0.01 to avoid time deviations
 * INITIAL_EXPLORATION_POINTS: The number of inputs to generate and test before the main fuzz loop begins.
 * EXECUTIONS_PER_PHI_STAGE: The number of consecutive optimizer-generated inputs to test during each phi stage.
 * MAX_EXECUTIONS_PER_LAMBDA_STAGE: The maximum number of mutations to make during each lambda stage.
 * HEADLESS_MODE: Run the fuzzing campaign with or without the Gazebo/MAVproxy GUIs. Mainly for debugging.
 */
const SYSTEM_UNDER_TEST: &str = "copter"; // Default out-of-box backend is configs/copter462.toml.
const MISSION_WAYPOINT_FILE_NAME: &str =
    "virtuals/physics/flight_controllers/courbet/mavlink/copter_mission.waypoints";
const MISSION_TIMEOUT: f64 = 220.0; // seconds
const POST_ARM_PARAMETER_UPDATE_DELAY: f64 = 0.0; // seconds
const NOISE_APPLICATION_SIM_TIME: f64 = 100.0; // seconds
const RECORDING_TIMESTEP: f64 = 0.01; // seconds
const INITIAL_EXPLORATION_POINTS: usize = 10;
const EXECUTIONS_PER_PHI_STAGE: usize = 10;
const MAX_EXECUTIONS_PER_LAMBDA_STAGE: usize = 10;
const HEADLESS_MODE: bool = true; // Keep the default Docker/native campaign headless.

fn optifuzz_vehicle() -> String {
    env::var("FASTDYN_OPTIFUZZ_VEHICLE").unwrap_or_else(|_| SYSTEM_UNDER_TEST.to_string())
}

fn optifuzz_mission_file() -> String {
    env::var("FASTDYN_OPTIFUZZ_MISSION_FILE")
        .unwrap_or_else(|_| MISSION_WAYPOINT_FILE_NAME.to_string())
}

fn optifuzz_env_flag(name: &str) -> bool {
    matches!(
        env::var(name)
            .unwrap_or_else(|_| "0".to_string())
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn run_optifuzz_smoke(input_library: &CPExpInput) {
    let param_values = input_library
        .get_param_input()
        .iter()
        .map(|param| {
            let range = param.get_range();
            ((range.0 + range.1) * 0.5) as f32
        })
        .collect::<Vec<f32>>();
    let param_names = input_library
        .get_param_input()
        .iter()
        .map(|param| param.get_name().as_str())
        .collect::<Vec<&str>>()
        .join(",");
    let input = TargetInput::new(TargetInput::f32_vec_to_bytes(&param_values), String::new());
    let vehicle = optifuzz_vehicle();
    let mission_file = optifuzz_mission_file();
    let result = execute_mission(
        &input,
        param_names,
        &vehicle,
        &mission_file,
        MISSION_TIMEOUT,
        POST_ARM_PARAMETER_UPDATE_DELAY,
        NOISE_APPLICATION_SIM_TIME,
        HEADLESS_MODE,
    );
    println!(
        "OptiFuzz smoke finished for vehicle={} with exit kind: {:?}",
        vehicle, result
    );
    if !matches!(result, ExitKind::Ok) {
        std::process::exit(1);
    }
}

pub fn main() {
    env_logger::init();
    if env::var("FASTDYN_OPTIFUZZ_BACKEND").is_err() {
        env::set_var("FASTDYN_OPTIFUZZ_BACKEND", "fmuv3");
    }

    /**
     * This code translates English descriptions of parameters into actual parameters using LLM.
     * For March NSWCPD Demonstration purposes, we omit this feature as it requires internet.
     */
    // let api_key = env::var("OPENAI_API_KEY")
    //     .expect("Set OPENAI_API_KEY in your environment");

    // let response = param_shim::get_parameter_shim_from_openai(
    //     &api_key,
    //     "ArduPilot",
    //     "Rover-4.6",
    // ).expect("Failed to get parameter shim from OpenAI");

    // let mut generated_param_input: Vec<ParamInput> = param_shim::parse_param_shim_response(&api_key, &response)
    //     .expect("Failed to parse parameter shim response");

    // // For debugging print out the generated parameter input
    // println!("Generated parameter inputs from OpenAI:");
    // for param in generated_param_input.iter() {
    //     println!("  - {}: {:?}", param.get_name(), param.get_range());
    // }

    // println!("OpenAI says: {}", response);
    let cvg_observer = unsafe { StdMapObserver::new("cvg", &mut CVG) }; // Example address
    let coverage_feedback_for_stats = MaxMapFeedback::new(&cvg_observer);

    /**
     * BUILDING THE FEEDBACK AND OBJECTIVE OBJECTS
     *
     * These objects define how the fuzzer evaluates input as "interesting" or a solution.
     * Interesting inputs (associated with feedback) are added to the corpus and will be mutated in the future.
     * Solutions (associated with objectives) are saved to the "crashes/" directory.
     *
     * Choose which metrics are used for feedback and objectives by adding them inside the `feedback_or!` macro.
     */
    let mut physical_feedback = PhysicalFeedback::new(); // Does the input lower an STL's minimum robustness?
    let mut coverage_feedback = coverage_feedback_for_stats.clone(); // Does the input increase code coverage?
    let crash_feedback = CrashFeedback::new(); // Does the input cause a hardfault in the firmware?
    let timeout_feedback = TimeoutFeedback::new(); // Does the input cause the mission to exceed the time limit?
    let physical_objective = PhysicalObjective::new(); // Does the input falsify an STL formula (robustness < 0)?

    // Modify these lines to adjust feedback/objective metrics
    let mut feedback = feedback_or!(physical_feedback,); // coverage_feedback,);
    let mut objective = feedback_or!(physical_objective, crash_feedback,);

    /**
     * DEFINING THE FUZZING INPUTS
     *
     * Here, you may define which inputs you wish to search during the fuzzing campaign.
     * We define two types of inputs: parameter inputs and environment inputs.
     * Parameter inputs are actual ArduPilot firmware parameters which are updated using MAVlink.
     * All other inputs are environment inputs. In this example, we have magnetometer and IMU sensor noise.
     *
     * With the parameter description to LLM functionality, you would not need to manually specify parameter inputs.
     * In any case, you may consult the following references below for ArduPilot parameter descriptions:
     *
     * https://ardupilot.org/rover/docs/parameters-Rover-stable-V4.6.2.html
     * https://ardupilot.org/plane/docs/parameters-Plane-stable-V4.6.2.html
     */
    // We have included the parameters we fuzzed during the ArduPlane campaign here as an example.
    let param_info_vec: Vec<ParamInput> = vec![
        ParamInput::new_float("ATC_RAT_YAW_P", (0.05, 0.5), 0.005),
        ParamInput::new_float("ATC_RAT_YAW_I", (0.0, 0.2), 0.005),
        ParamInput::new_float("ATC_RAT_YAW_D", (0.0, 0.03), 0.001),
        ParamInput::new_float("ATC_RAT_YAW_FF", (0.0, 0.2), 0.005),
        // Group 1 — Roll Control Loop
        // ParamInput::new_float("RLL_RATE_P", (0.08, 0.35), 0.005),
        // ParamInput::new_float("RLL_RATE_I", (0.01, 0.6), 0.01),
        // ParamInput::new_float("RLL_RATE_D", (0.01, 0.6), 0.01),
        // ParamInput::new_float("RLL_RATE_SMAX", (0.0, 200.0), 0.5),
        // ParamInput::new_float("RLL2SRV_RMAX", (0.0, 180.0), 1.0),

        // Group 2 — Pitch Control Loop
        // ParamInput::new_float("PTCH_RATE_P", (0.08, 0.35), 0.005),
        // ParamInput::new_float("PTCH_RATE_I", (0.01, 0.6), 0.01),
        // ParamInput::new_float("PTCH_RATE_D", (0.001, 0.03), 0.001),
        // ParamInput::new_float("PTCH_RATE_SMAX", (0.0, 200.0), 0.5),
        // ParamInput::new_float("PTCH2SRV_RMAX_UP", (0.0, 100.0), 1.0),
        // ParamInput::new_float("PTCH2SRV_RMAX_DN", (0.0, 100.0), 1.0),

        // Group 3 — Energy/Throttle Management
        // ParamInput::new_float("THR_MAX", (0.0, 100.0), 1.00),
        // ParamInput::new_float("THR_MIN", (-100.0, 100.0), 1.00),
        // ParamInput::new_float("THR_SLEWRATE", (0.0, 127.0), 1.00),
        // ParamInput::new_float("KFF_THR2PTCH", (-5.0, 5.0), 0.01),

        // Group 4 — Flight Envelope Limits
        // ParamInput::new_float("LEVEL_ROLL_LIMIT", (0.0, 45.0), 1.0),
        // ParamInput::new_float("PTCH_LIM_MAX_DEG", (0.0, 90.0), 1.0),
        // ParamInput::new_float("PTCH_LIM_MIN_DEG", (-90.0, 0.0), 1.0),

        // Group 5 — Navigation / Path Following
        // ParamInput::new_float("WP_LOITER_RAD", (-32767.0, 32767.0), 1.0),
        // ParamInput::new_float("WP_MAX_RADIUS", (0.0, 32767.0), 1.0),
        // ParamInput::new_float("FBWB_CLIMB_RATE", (1.0, 10.0), 0.1),

        // Group 6 — Sensor / Estimation Perturbation
        // ParamInput::new_float("GPS1_MB_OFS_X", (-5.0, 5.0), 0.01),
        // ParamInput::new_float("GPS1_MB_OFS_Y", (-5.0, 5.0), 0.01),
        // ParamInput::new_float("GPS1_MB_OFS_Z", (-5.0, 5.0), 0.01),
        // ParamInput::new_float("INS_POS1_X", (-5.0, 5.0), 0.01),
        // ParamInput::new_float("INS_POS1_Y", (-5.0, 5.0), 0.01),
        // ParamInput::new_float("INS_POS1_Z", (-5.0, 5.0), 0.01),
    ];

    let env_info_vec: Vec<EnvInput> = vec![
        // Group 1 - IMU gyroscope noise
        // EnvInput::new("gyro_white_scale", (0.0, 5.0), false),
        // EnvInput::new("gyro_drift_scale", (0.0, 5.0), false),

        // Group 2 - IMU accelerometer noise
        // EnvInput::new("accel_white_scale", (0.0, 5.0), false),
        // EnvInput::new("accel_drift_scale", (0.0, 5.0), false),

        // Group 3 - Magnetometer noise
        // EnvInput::new("mag_white_scale", (0.0, 5.0), false),
        // EnvInput::new("mag_drift_scale", (0.0, 5.0), false),
    ];

    // Create optimizer
    let input_library = CPExpInput::new(param_info_vec, env_info_vec);
    if optifuzz_env_flag("FASTDYN_OPTIFUZZ_SMOKE") {
        run_optifuzz_smoke(&input_library);
        return;
    }

    let space = search_space_from_input_library(&input_library);
    let mut opt = BayesianOptimizationOptions::default();
    opt.n_initial_points = INITIAL_EXPLORATION_POINTS;
    let mut bo = BayesianOptimizer::new(space, Some(opt));

    let mut state = CPExpState::new(
        // RNG
        StdRand::with_seed(current_nanos()),
        // Corpus that will be evolved, we keep it in memory for performance
        // InMemoryCorpus::new(),
        OnDiskCorpus::new(PathBuf::from(CORPUS_LOG_PATH)).unwrap(),
        // Corpus in which we store solutions (crashes in this example),
        // on disk so the user can get them after stopping the fuzzer
        OnDiskCorpus::new(PathBuf::from(CRASH_LOG_PATH)).unwrap(),
        // States of the feedbacks.
        // The feedbacks can report the data that should persist in the State.
        &mut feedback,
        // Same for objective feedbacks
        &mut objective,
        // Bayesian Optimizer!
        Some(bo),
        // Start with phi stage first?
        // false,
        // CPExpInput object for transforming TargetInputs (serializable) into usable values
        input_library,
    )
    .unwrap();

    // The Monitor trait define how the fuzzer stats are displayed to the user
    #[cfg(not(feature = "tui"))]
    let mon = SimpleMonitor::new(|s| println!("{s}"));
    #[cfg(feature = "tui")]
    let mon = TuiMonitor::builder()
        .title("Baby Fuzzer")
        .enhanced_graphics(false)
        .build();

    // The event manager handle the various events generated during the fuzzing loop
    // such as the notification of the addition of a new item to the corpus
    let mut mgr = SimpleEventManager::new(mon);

    // A queue policy to get testcasess from the corpus
    let scheduler = QueueScheduler::new();

    // A fuzzer with feedbacks and a corpus scheduler
    let mut fuzzer = StdFuzzer::new(scheduler, feedback, objective);

    let physical_observer = PhysicalObserver::new(
        RECORDING_TIMESTEP,
        MISSION_TIMEOUT,
        TRACE_LOG_PATH,
        ROBUSTNESS_LOG_PATH,
    );

    let executor = FastDynExecutor::new(&state);
    let mut executor = WithObservers::new(executor, tuple_list!(physical_observer, cvg_observer));

    // Generate initial inputs using the optimizer
    state
        .generate_initial_inputs(
            &mut fuzzer,
            &mut executor,
            &mut mgr,
            INITIAL_EXPLORATION_POINTS,
        )
        .expect("Failed to generate the initial corpus");

    println!("Starting the fuzzing loop!");

    let stats_stage = AflStatsStage::builder()
        .map_feedback(&coverage_feedback_for_stats)
        .build()
        .unwrap();

    // Setup a mutational stage with a basic bytes mutator
    let mutator = HavocScheduledMutator::new(havoc_mutations());

    let mut stages = tuple_list!(
        PhiStage::new(EXECUTIONS_PER_PHI_STAGE),
        LambdaMutationalStage::with_max_iterations(
            mutator,
            nonzero!(MAX_EXECUTIONS_PER_LAMBDA_STAGE)
        ),
        stats_stage,
    );

    fuzzer
        .fuzz_loop(&mut stages, &mut executor, &mut state, &mut mgr)
        .expect("Error in the fuzzing loop");
}
