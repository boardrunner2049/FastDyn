#[cfg(feature = "gazebo")]
use baby_fuzzer::gz_state_parser::{
    apply_noise, extract_block_from_gz_data, get_raw_gz_data, get_raw_hf_status, get_sim_time,
};

use crate::cpexp_input::TargetInput;
use crate::CVG;

#[cfg(feature = "gazebo")]
use gz_transport::Node;
use libafl::executors::ExitKind;
use libafl::inputs::HasTargetBytes;
use std::env;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

#[cfg(feature = "gazebo")]
const RUN_SERVICES_DIR: &str = "../../physics/flight_controllers/courbet/gazebo";
#[cfg(feature = "gazebo")]
const MAV_C2_DIR: &str = "../../physics/flight_controllers/courbet/mavlink";
const FASTDYN_DIR: &str = "../../..";

fn fastdyn_dir() -> PathBuf {
    let default_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(FASTDYN_DIR);
    let root = env::var("FASTDYN_ROOT")
        .map(PathBuf::from)
        .unwrap_or(default_root);
    fs::canonicalize(&root).unwrap_or(root)
}

fn optifuzz_backend(cps_name: &str) -> String {
    env::var("FASTDYN_OPTIFUZZ_BACKEND").unwrap_or_else(|_| {
        if matches!(cps_name, "copter" | "rover" | "plane") {
            "fmuv3".to_string()
        } else {
            "gazebo".to_string()
        }
    })
}

fn execution_work_dir() -> PathBuf {
    let execution = env::var("EXECUTION").unwrap_or_else(|_| "0".to_string());
    let fastdyn_root = fastdyn_dir();
    let root = env::var("FASTDYN_OPTIFUZZ_WORK_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|_| fastdyn_root.join("out/optifuzz"));
    let root = if root.is_absolute() {
        root
    } else {
        fastdyn_root.join(root)
    };
    root.join(format!("execution-{}", execution))
}

fn write_mutations(input: &TargetInput, work_dir: &Path) -> PathBuf {
    fs::create_dir_all(work_dir).expect("failed to create OptiFuzz work directory");
    let mutation_path = work_dir.join("mutations.bin");
    let raw_parameter_values = input.get_param_bytes().target_bytes().clone();
    let mut bin_param_file: File = File::create(&mutation_path).unwrap();
    let _ = bin_param_file.write_all(&raw_parameter_values).unwrap();
    let _ = bin_param_file.sync_all().unwrap();
    mutation_path
}

/**
 * Assigning each value in file to corresponding index in CVG array.
 * File is of the format: "value,value,..." where each value is u8.
 */
fn deserialize_coverage(path: &str) {
    let contents = match std::fs::read_to_string(path) {
        Ok(contents) => contents,
        Err(err) => {
            println!("Warning: coverage file {path} was not available: {err}");
            return;
        }
    };
    let values: Vec<&str> = contents.trim().split(',').collect();
    unsafe {
        for (i, val_str) in values.iter().enumerate() {
            if i >= CVG.len() {
                break;
            } else {
                let val: u8 = val_str.parse().unwrap_or(0);
                if CVG[i] + val > 255 {
                    CVG[i] = 255;
                } else {
                    CVG[i] += val;
                }
            }
        }
    }
}

fn env_or_default(name: &str, default: &str) -> String {
    env::var(name).unwrap_or_else(|_| default.to_string())
}

fn env_flag_enabled(value: &str) -> bool {
    matches!(
        value.to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn maybe_env(command: &mut Command, name: &str, value: String) {
    if !value.is_empty() {
        command.env(name, value);
    }
}

fn execute_fmuv3_mission(
    input: &TargetInput,
    param_names: String,
    cps_name: &str,
    mission_file_name: &str,
) -> ExitKind {
    let work_dir = execution_work_dir();
    let mutation_path = write_mutations(input, &work_dir);
    let config = env_or_default(
        "FASTDYN_OPTIFUZZ_CONFIG",
        &format!("configs/{}462.toml", cps_name),
    );
    let swarm_instances = env::var("FASTDYN_OPTIFUZZ_SWARM_INSTANCES")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(1);
    let coverage_enabled = env_or_default("FASTDYN_OPTIFUZZ_COVERAGE", "0");

    let mut command = Command::new("fastdyn");
    command.current_dir(fastdyn_dir());
    if swarm_instances > 1 {
        let base_port = env_or_default("FASTDYN_OPTIFUZZ_BASE_PORT", "19000");
        command
            .arg("swarm")
            .arg("-c")
            .arg(&config)
            .arg("-n")
            .arg(swarm_instances.to_string())
            .arg("-o")
            .arg(work_dir.join("swarm"))
            .arg("--jobs")
            .arg(swarm_instances.to_string())
            .arg("--base-port")
            .arg(base_port);
    } else {
        command
            .arg("run")
            .arg("-c")
            .arg(&config)
            .arg("-p")
            .arg("-o")
            .arg(&work_dir);
    }

    command
        .env("FASTDYN_OPTIFUZZ", "1")
        .env("FASTDYN_OPTIFUZZ_MUTATION_BIN", &mutation_path)
        .env("FASTDYN_OPTIFUZZ_PARAM_NAMES", param_names)
        .env("FASTDYN_QEMU_MEMORY_DIR", work_dir.join("qemu-memory"))
        .env("FASTDYN_COVERAGE", &coverage_enabled)
        .env("FASTDYN_COVERAGE_FILE", work_dir.join("cvg.bin"))
        .env("FASTDYN_BBL_FILE", work_dir.join("bbl.txt"));

    if !mission_file_name.is_empty() {
        maybe_env(
            &mut command,
            "FASTDYN_MISSION_FILE",
            mission_file_name.to_string(),
        );
    }
    if let Ok(param_file) = env::var("FASTDYN_OPTIFUZZ_PARAM_FILE") {
        maybe_env(&mut command, "FASTDYN_PARAM_FILE", param_file);
    }

    if env_or_default("FASTDYN_OPTIFUZZ_VERBOSE", "0") != "1" {
        command.stdout(Stdio::null()).stderr(Stdio::null());
    }

    if env_or_default("FASTDYN_OPTIFUZZ_DRY_RUN", "0") == "1" {
        println!("OptiFuzz FMUv3 dry-run command: {:?}", command);
        if env_flag_enabled(&coverage_enabled) {
            let _ = fs::write(work_dir.join("cvg.bin"), "");
            deserialize_coverage(work_dir.join("cvg.bin").to_string_lossy().as_ref());
        }
        return ExitKind::Ok;
    }

    let status = command
        .status()
        .expect("Error: Failed to start FastDyn FMUv3 run");
    if env_flag_enabled(&coverage_enabled) {
        deserialize_coverage(work_dir.join("cvg.bin").to_string_lossy().as_ref());
    }
    if status.success() {
        ExitKind::Ok
    } else {
        ExitKind::Timeout
    }
}

/*
    Steps:
    1. Apply inputs
    2. Start Courbet services
        - run_and_attach_services.sh
        - mav_command_and_control.py
        - fd_rover.sh
    3. Monitor for end states and return an ExitKind accordingly:
        - Mission complete (Mavlink C2 exits without error) = ExitKind::Ok
        - Crash (/get_hf_status says "true") = ExitKind::Crash
        - Timeout (sim time from /clock) = ExitKind::Timeout
*/

pub fn execute_mission(
    input: &TargetInput,
    param_names: String,
    cps_name: &str,
    mission_file_name: &str,
    timeout: f64,
    param_input_delay: f64,
    noise_time: f64,
    headless: bool,
) -> ExitKind {
    if optifuzz_backend(cps_name) == "fmuv3" {
        return execute_fmuv3_mission(input, param_names, cps_name, mission_file_name);
    }

    #[cfg(feature = "gazebo")]
    return execute_gazebo_mission(
        input, param_names, cps_name, mission_file_name,
        timeout, param_input_delay, noise_time, headless,
    );

    #[cfg(not(feature = "gazebo"))]
    {
        let _ = (timeout, param_input_delay, noise_time, headless);
        panic!("the Gazebo backend requires building with --features gazebo");
    }
}

#[cfg(feature = "gazebo")]
fn execute_gazebo_mission(
    input: &TargetInput,
    param_names: String,
    cps_name: &str,
    mission_file_name: &str,
    timeout: f64,
    param_input_delay: f64,
    noise_time: f64,
    headless: bool,
) -> ExitKind {

    // 1. Apply inputs
    // println!("execute_mission received inputs: {:?}", input);
    let raw_env_config = input.get_env_config();
    let work_dir = execution_work_dir();
    let mutation_path = write_mutations(input, &work_dir);

    // 2. Start Courbet services
    let mut service_binding = Command::new("./run_and_attach_services.sh");
    let services_command = service_binding.current_dir(RUN_SERVICES_DIR).arg(cps_name);
    if headless {
        services_command.arg("headless");
    }

    let spawn_services = services_command.stdout(Stdio::null()).spawn();
    if spawn_services.is_err() {
        panic!(
            "Error: Failed to start services: {}",
            spawn_services.err().unwrap()
        );
    }

    // Let services spin up
    std::thread::sleep(std::time::Duration::from_secs(5));

    let init_file_name: &str;
    if cps_name == "rover" {
        init_file_name = "rover_init.param";
    } else if cps_name == "plane" {
        init_file_name = "plane_init.parm";
    } else {
        panic!("Error: Invalid CPS name: {}", cps_name);
    }

    let mut py_binding = Command::new("python3");
    let c2_command = py_binding
        .current_dir(MAV_C2_DIR)
        .arg("ardu_mav_c2.py")
        .arg(init_file_name)
        .arg(mission_file_name)
        .arg(param_names)
        .arg(param_input_delay.to_string())
        .arg(mutation_path);
    if headless {
        c2_command.arg("headless");
    }

    let mut spawn_c2 = c2_command.spawn();
    if spawn_c2.is_err() {
        panic!(
            "Error: Failed to start ardu_mav_c2.py: {}",
            spawn_c2.err().unwrap()
        );
    }

    // Let mavlink C2 spin up
    std::thread::sleep(std::time::Duration::from_secs(5));

    let script_name: String = format!("configs/{}462.toml", cps_name);
    let spawn_fd = Command::new("fastdyn")
        .current_dir(FASTDYN_DIR)
        .arg("run")
        .arg("-c")
        .arg(script_name)
        .stdout(Stdio::null())
        .spawn();
    if spawn_fd.is_err() {
        panic!(
            "Error: Failed to start FastDyn QEMU: {}",
            spawn_fd.err().unwrap()
        );
    }

    // 3. Monitor for end states in order of priority
    let mut node: Node = Node::new().unwrap();
    let exit_kind: ExitKind;
    let mut noise_applied: bool = false;
    loop {
        // Check for hard fault
        let hf_status: String = get_raw_hf_status(&mut node);
        if hf_status.is_empty() {
            println!("Warning: Failed to get hf_status from Gazebo!");
            exit_kind = ExitKind::Ok;
            break;
        } else if hf_status.find("true").is_some() {
            exit_kind = ExitKind::Crash;
            break;
        }

        // Check for timeout
        let gz_data = get_raw_gz_data(&mut node);
        if gz_data.is_empty() {
            println!("Warning: Failed to get gz_data from Gazebo!");
            exit_kind = ExitKind::Ok;
            break;
        } else {
            let clock_block = extract_block_from_gz_data(&gz_data, "clock");
            let sim_time: f64 = get_sim_time(&clock_block);
            if sim_time >= timeout {
                exit_kind = ExitKind::Timeout;
                break;
            } else if !noise_applied && sim_time >= noise_time {
                apply_noise(raw_env_config.clone(), &mut node, sim_time);
                noise_applied = true;
            }
        }

        // Check for mission complete
        let status = spawn_c2.as_mut().unwrap().try_wait().unwrap();
        if status.is_some() {
            // Mavlink C2 has exited. If the exit code is 0, mission complete.
            if status.unwrap().success() {
                exit_kind = ExitKind::Ok;
                break;
            } else {
                // Timeout
                exit_kind = ExitKind::Timeout;
                break;
            }
        }
    }

    // Just use kill script to force everything dead
    let mut kill = Command::new("./kill.sh")
        .spawn()
        .expect("Failed to spawn kill.sh");
    let _ = kill.wait();

    // Clean up zombies
    let _ = spawn_services.unwrap().wait();
    let _ = spawn_c2.unwrap().wait();
    let _ = spawn_fd.unwrap().wait();

    let cvg_path = format!("{}/fastdyn_work/cvg.bin", FASTDYN_DIR);
    deserialize_coverage(&cvg_path);

    println!("Mission execution finished with exit kind: {:?}", exit_kind);
    exit_kind
}
