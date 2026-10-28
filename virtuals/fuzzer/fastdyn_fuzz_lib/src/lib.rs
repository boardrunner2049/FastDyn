#[cfg(windows)]
use std::ptr::write_volatile;
use std::{
    fs::{self, OpenOptions},
    io::Write,
    marker::PhantomData,
    path::{Path, PathBuf},
    ptr,
    sync::{
        atomic::{AtomicBool, AtomicPtr, AtomicU32, AtomicU64, AtomicUsize, Ordering},
        Mutex,
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};

use core::num::NonZeroUsize;
#[cfg(feature = "tui")]
use libafl::monitors::tui::TuiMonitor;
#[cfg(feature = "tui")]
use libafl::monitors::OnDiskJsonMonitor;
#[cfg(not(feature = "tui"))]
use libafl::monitors::{OnDiskJsonMonitor, SimpleMonitor};
use libafl::{
    common::HasNamedMetadata,
    corpus::{Corpus, InMemoryCorpus, OnDiskCorpus},
    events::SimpleEventManager,
    executors::{Executor, ExitKind, WithObservers},
    feedback_and_fast, feedback_or_fast,
    feedbacks::{CrashFeedback, MapFeedbackMetadata, MaxMapFeedback, TimeoutFeedback},
    fuzzer::Fuzzer,
    generators::RandPrintablesGenerator,
    inputs::{BytesInput, HasTargetBytes},
    mutators::{havoc_mutations::havoc_mutations, scheduled::HavocScheduledMutator},
    observers::StdMapObserver,
    schedulers::QueueScheduler,
    stages::{mutational::StdMutationalStage, AflStatsStage, CalibrationStage, Restartable, Stage},
    state::{HasCorpus, HasExecutions, StdState},
    BloomInputFilter, StdFuzzerBuilder,
};
use libafl_bolts::{current_nanos, rands::StdRand, tuples::tuple_list, AsSlice};

static STOP_FLAG: AtomicBool = AtomicBool::new(false);
static ASSERT_STATUS: AtomicU32 = AtomicU32::new(ASSERT_NONE);
static CURRENT_INPUT_PTR: AtomicPtr<u8> = AtomicPtr::new(ptr::null_mut());
static CURRENT_INPUT_LEN: AtomicUsize = AtomicUsize::new(0);
static NEXT_INPUT_EPOCH: AtomicU64 = AtomicU64::new(1);
static PUBLISHED_INPUT_EPOCH: AtomicU64 = AtomicU64::new(0);
static COMPLETED_INPUT_EPOCH: AtomicU64 = AtomicU64::new(0);
static BACKEND_WAITING_EPOCH: AtomicU64 = AtomicU64::new(0);
static INTERESTING_TRACE_COUNT: AtomicUsize = AtomicUsize::new(0);

// These counters deliberately live outside LibAFL state. They remain visible
// while QEMU is blocked at the sync callback and identify which half of the
// producer/consumer handshake stopped making progress.
static FUZZ_CYCLES: AtomicU64 = AtomicU64::new(0);
static EXECUTOR_RUNS: AtomicU64 = AtomicU64::new(0);
static BACKEND_WAIT_CALLS: AtomicU64 = AtomicU64::new(0);
static BACKEND_DELIVERIES: AtomicU64 = AtomicU64::new(0);
static BACKEND_COMPLETIONS: AtomicU64 = AtomicU64::new(0);
static BACKEND_WAITING_FOR_EPOCH: AtomicU64 = AtomicU64::new(0);

const PROGRESS_LOG_INTERVAL: Duration = Duration::from_secs(30);

lazy_static::lazy_static! {
    static ref FUZZ_THREAD: Mutex<Option<JoinHandle<()>>> = Mutex::new(None);
}

//This will hold the coverage from the run.
const MAP_SIZE: usize = 65536; // same as AFL, make sure the definition of this size in C is the same
#[no_mangle]
pub static mut CVG: [u8; MAP_SIZE] = [0; MAP_SIZE];

const ASSERT_NONE: u32 = 0;
const ASSERT_RECOVERABLE: u32 = 1;
const ASSERT_FATAL: u32 = 2;
const ASSERT_TIMEOUT: u32 = 3;

#[repr(C)]
struct FastDynTraceRun {
    count: u32,
    capacity: u32,
    entries: *const u32,
}

extern "C" {
    fn fuzz_dump_bbl();
    fn fuzz_trace_commit_run();
    fn fuzz_trace_disable();
    fn fuzz_trace_enable(max_entries: i32);
    fn fuzz_trace_reset();

    static mut g_trace_completed: FastDynTraceRun;
    static mut g_trace_enabled: bool;
}

fn spin_wait_until(mut done: impl FnMut() -> bool) {
    const SPIN_ITERS: usize = 10_000;
    let mut spins = 0;

    while !done() {
        if spins < SPIN_ITERS {
            std::hint::spin_loop();
            spins += 1;
        } else {
            std::thread::yield_now();
        }
    }
}

fn publish_input(epoch: u64, buf: &[u8]) {
    ASSERT_STATUS.store(ASSERT_NONE, Ordering::Release);
    CURRENT_INPUT_LEN.store(buf.len(), Ordering::Relaxed);
    CURRENT_INPUT_PTR.store(buf.as_ptr() as *mut u8, Ordering::Release);
    PUBLISHED_INPUT_EPOCH.store(epoch, Ordering::Release);
}

fn clear_input() {
    CURRENT_INPUT_PTR.store(ptr::null_mut(), Ordering::Release);
    CURRENT_INPUT_LEN.store(0, Ordering::Relaxed);
}

fn log_progress(context: &str) {
    eprintln!(
        "[fastdyn-progress] {context}: bloom_filter=disabled fuzz_cycles={} executor_runs={} backend_waits={} deliveries={} completions={} waiting_for_epoch={} published_epoch={} completed_epoch={}",
        FUZZ_CYCLES.load(Ordering::Relaxed),
        EXECUTOR_RUNS.load(Ordering::Relaxed),
        BACKEND_WAIT_CALLS.load(Ordering::Relaxed),
        BACKEND_DELIVERIES.load(Ordering::Relaxed),
        BACKEND_COMPLETIONS.load(Ordering::Relaxed),
        BACKEND_WAITING_FOR_EPOCH.load(Ordering::Acquire),
        PUBLISHED_INPUT_EPOCH.load(Ordering::Acquire),
        COMPLETED_INPUT_EPOCH.load(Ordering::Acquire),
    );
}

/// Runs after mutation once per selected corpus entry.
struct ProgressStage {
    last_report: Instant,
    completed_cycles: u64,
}

impl ProgressStage {
    fn new() -> Self {
        Self {
            last_report: Instant::now(),
            completed_cycles: 0,
        }
    }
}

impl<E, EM, S, Z> Stage<E, EM, S, Z> for ProgressStage {
    fn perform(
        &mut self,
        _fuzzer: &mut Z,
        _executor: &mut E,
        _state: &mut S,
        _manager: &mut EM,
    ) -> Result<(), libafl::Error> {
        self.completed_cycles += 1;
        FUZZ_CYCLES.store(self.completed_cycles, Ordering::Relaxed);
        if self.last_report.elapsed() >= PROGRESS_LOG_INTERVAL {
            log_progress(&format!("completed_fuzz_cycles={}", self.completed_cycles));
            self.last_report = Instant::now();
        }
        Ok(())
    }
}

impl<S> Restartable<S> for ProgressStage {
    fn should_restart(&mut self, _state: &mut S) -> Result<bool, libafl::Error> {
        Ok(true)
    }

    fn clear_progress(&mut self, _state: &mut S) -> Result<(), libafl::Error> {
        Ok(())
    }
}

fn wait_until_complete(epoch: u64) -> Result<(), ()> {
    const SPIN_ITERS: usize = 10_000;
    let timeout = initial_input_wait_timeout();
    let start_time = Instant::now();
    let mut spins = 0;

    while COMPLETED_INPUT_EPOCH.load(Ordering::Acquire) < epoch {
        if start_time.elapsed() > timeout {
            log_progress("target_completion_timeout");
            return Err(());
        }

        if spins < SPIN_ITERS {
            std::hint::spin_loop();
            spins += 1;
        } else {
            std::thread::yield_now();
        }
    }

    Ok(())
}

fn initial_input_wait_timeout() -> Duration {
    const DEFAULT_SECONDS: u64 = 5;

    match std::env::var("FASTDYN_INPUT_WAIT_TIMEOUT_SECONDS") {
        Ok(value) => match value.parse::<u64>() {
            Ok(seconds) if seconds > 0 => Duration::from_secs(seconds),
            _ => {
                eprintln!(
                    "Ignoring FASTDYN_INPUT_WAIT_TIMEOUT_SECONDS={value:?}; expected a positive integer"
                );
                Duration::from_secs(DEFAULT_SECONDS)
            }
        },
        Err(_) => Duration::from_secs(DEFAULT_SECONDS),
    }
}

fn wait_until_backend_ready(epoch: u64) -> Result<(), ()> {
    const SPIN_ITERS: usize = 10_000;
    let timeout = initial_input_wait_timeout();
    let start_time = Instant::now();
    let mut spins = 0;

    while BACKEND_WAITING_EPOCH.load(Ordering::Acquire) < epoch {
        if start_time.elapsed() > timeout {
            log_progress("backend_ready_timeout");
            return Err(());
        }

        if spins < SPIN_ITERS {
            std::hint::spin_loop();
            spins += 1;
        } else {
            std::thread::yield_now();
        }
    }

    Ok(())
}

fn take_assert_status() -> u32 {
    ASSERT_STATUS.swap(ASSERT_NONE, Ordering::AcqRel)
}

// ------------------ Tracing and Interesting tracking for LLM augmentation ----------------------
pub fn maxmapfeedback_last_input_interesting<S>(state: &S) -> bool
where
    S: HasNamedMetadata,
{
    let Ok(metadata) = state.named_metadata::<MapFeedbackMetadata<u8>>("cvg") else {
        return false;
    };

    let history = metadata.history_map.as_slice();
    let cvg = core::ptr::addr_of!(CVG) as *const u8;

    for i in 0..MAP_SIZE {
        let current = unsafe { *cvg.add(i) };
        let previous = history.get(i).copied().unwrap_or_default();

        if current > previous {
            return true;
        }
    }

    false
}

pub fn trace_input_once<I>(input: &I) -> u32
where
    I: HasTargetBytes,
{
    let target = input.target_bytes();
    let buf = target.as_slice();
    let epoch = NEXT_INPUT_EPOCH.fetch_add(1, Ordering::Relaxed);

    if wait_until_backend_ready(epoch).is_err() {
        return ASSERT_TIMEOUT;
    }

    unsafe {
        fuzz_trace_reset();
        fuzz_trace_enable(-1);
    }

    publish_input(epoch, buf);
    let wait_result = wait_until_complete(epoch);
    clear_input();

    unsafe {
        fuzz_trace_disable();
        fuzz_trace_commit_run();
        //g_trace_enabled = false;
    }

    if wait_result.is_err() {
        return ASSERT_TIMEOUT;
    }

    take_assert_status()
}

fn write_completed_trace(
    trace_path: &Path,
    capture_name: &str,
    source_path: Option<&Path>,
    status: u32,
) -> std::io::Result<()> {
    let mut file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(trace_path)?;

    writeln!(file, "# {capture_name}")?;
    if let Some(source_path) = source_path {
        writeln!(file, "# source={}", source_path.display())?;
    }
    writeln!(file, "# assert_status={}", status)?;

    unsafe {
        let trace = core::ptr::addr_of!(g_trace_completed);
        let count = (*trace).count as usize;

        let entries = (*trace).entries;

        if count == 0 || entries.is_null() {
            writeln!(file, "# empty trace")?;
        } else {
            for i in 0..count {
                writeln!(file, "0x{:08x}", *entries.add(i))?;
            }
        }
    }

    Ok(())
}

pub fn trace_interesting_write_completed_trace<I>(input: &I, status: u32) -> std::io::Result<()>
where
    I: HasTargetBytes,
{
    fs::create_dir_all("fastdyn_work/corpus")?;

    let trace_id = INTERESTING_TRACE_COUNT.fetch_add(1, Ordering::Relaxed);
    let raw_path = format!("fastdyn_work/corpus/interesting{}.raw", trace_id);
    let trace_path = format!("fastdyn_work/corpus/interesting{}.trace", trace_id);

    std::fs::write(raw_path, input.target_bytes().as_slice())?;
    write_completed_trace(
        Path::new(&trace_path),
        "interesting input trace capture",
        None,
        status,
    )
}

fn trace_corpus_write_completed_trace(source_path: &Path, status: u32) -> std::io::Result<PathBuf> {
    let mut trace_path = source_path.as_os_str().to_os_string();
    trace_path.push(".trace");
    let trace_path = PathBuf::from(trace_path);

    write_completed_trace(
        &trace_path,
        "initial corpus input trace capture",
        Some(source_path),
        status,
    )?;

    Ok(trace_path)
}

fn next_interesting_trace_index(corpus_path: &std::path::Path) -> usize {
    fs::read_dir(corpus_path)
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let file_name = entry.file_name();
            let file_name = file_name.to_str()?;
            let suffix = file_name.strip_prefix("interesting")?;
            let index = suffix
                .strip_suffix(".raw")
                .or_else(|| suffix.strip_suffix(".trace"))?;
            index.parse::<usize>().ok()
        })
        .max()
        .map_or(0, |index| index.saturating_add(1))
}
// ------------------------ End of LLM stuff ------------------------------------

fn log_fatal_input(buf: &[u8]) {
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open("./fastdyn_work/fuzzer.log")
        .expect("Failed to open file");

    writeln!(file, "Fatal error on input {:?}", buf).unwrap();

    unsafe {
        fuzz_dump_bbl();
    }
}

fn log_timeout_input(buf: &[u8]) {
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open("./fastdyn_work/timeout.log")
        .expect("Failed to open file");

    writeln!(file, "Timeout on input {:?}", buf).unwrap();

    unsafe {
        fuzz_dump_bbl();
    }
}

#[no_mangle]
pub extern "C" fn fuzz_libafl_wait_next(
    after_epoch: u64,
    data: *mut *const u8,
    len: *mut usize,
) -> u64 {
    BACKEND_WAIT_CALLS.fetch_add(1, Ordering::Relaxed);
    BACKEND_WAITING_FOR_EPOCH.store(after_epoch.saturating_add(1), Ordering::Release);
    BACKEND_WAITING_EPOCH.store(after_epoch.saturating_add(1), Ordering::Release);
    spin_wait_until(|| PUBLISHED_INPUT_EPOCH.load(Ordering::Acquire) > after_epoch);

    let epoch = PUBLISHED_INPUT_EPOCH.load(Ordering::Acquire);
    let input_ptr = CURRENT_INPUT_PTR.load(Ordering::Acquire) as *const u8;
    let input_len = CURRENT_INPUT_LEN.load(Ordering::Relaxed);

    if data.is_null() || len.is_null() || input_ptr.is_null() {
        return 0;
    }

    unsafe {
        *data = input_ptr;
        *len = input_len;
    }

    BACKEND_DELIVERIES.fetch_add(1, Ordering::Relaxed);
    epoch
}

#[no_mangle]
pub extern "C" fn fuzz_libafl_complete(epoch: u64) {
    BACKEND_COMPLETIONS.fetch_add(1, Ordering::Relaxed);
    COMPLETED_INPUT_EPOCH.fetch_max(epoch, Ordering::AcqRel);
}

#[no_mangle]
pub extern "C" fn fuzz_libafl_report_assert(fatal: bool) {
    let status = if fatal {
        ASSERT_FATAL
    } else {
        ASSERT_RECOVERABLE
    };

    ASSERT_STATUS.fetch_max(status, Ordering::AcqRel);
}

struct FastDynExecutor<S> {
    phantom: PhantomData<S>,
    crashed: bool,
    pending_initial_traces: Vec<(PathBuf, BytesInput)>,
}

impl<S> FastDynExecutor<S> {
    pub fn new(_state: &S, pending_initial_traces: Vec<(PathBuf, BytesInput)>) -> Self {
        Self {
            phantom: PhantomData,
            crashed: false,
            pending_initial_traces,
        }
    }

    fn trace_initial_corpus_inputs(&mut self) {
        if self.pending_initial_traces.is_empty() {
            return;
        }

        if wait_until_backend_ready(NEXT_INPUT_EPOCH.load(Ordering::Acquire)).is_err() {
            eprintln!(
                "Timed out waiting for the backend before tracing the initial corpus. Exiting."
            );
            std::process::exit(1);
        }

        for (source_path, input) in std::mem::take(&mut self.pending_initial_traces) {
            let status = trace_input_once(&input);
            let trace_path = match trace_corpus_write_completed_trace(&source_path, status) {
                Ok(trace_path) => trace_path,
                Err(err) => {
                    eprintln!(
                        "Failed to write initial corpus trace for {}: {:?}",
                        source_path.display(),
                        err
                    );
                    source_path.clone()
                }
            };

            if matches!(status, ASSERT_RECOVERABLE | ASSERT_FATAL | ASSERT_TIMEOUT) {
                eprintln!(
                    "Initial corpus trace for {} ended with status {}; trace: {}. Exiting.",
                    source_path.display(),
                    status,
                    trace_path.display()
                );
                std::process::exit(1);
            }
        }
    }
}

impl<EM, I, S, Z> Executor<EM, I, S, Z> for FastDynExecutor<S>
where
    S: HasCorpus<I> + HasExecutions + HasNamedMetadata,
    I: HasTargetBytes,
{
    fn run_target(
        &mut self,
        _fuzzer: &mut Z,
        state: &mut S,
        _mgr: &mut EM,
        input: &I,
    ) -> Result<ExitKind, libafl::Error> {
        if self.crashed {
            // Exit only after LibAFL observes the previous input outcome.
            // Campaign state intentionally remains in memory: stale state.bin
            // files have caused later runs to resume an incompatible campaign.
            panic!("Fuzzer cannot continue, reached a hard fault or timeout");
        }

        // Trace re-execution is intentionally disabled.  Each trace capture
        // performs another target round-trip and can perturb the fuzzing
        // handshake independently of the normal input execution.

        EXECUTOR_RUNS.fetch_add(1, Ordering::Relaxed);

        // We need to keep track of the exec count.
        *state.executions_mut() += 1;

        let target = input.target_bytes();
        let buf = target.as_slice();
        let epoch = NEXT_INPUT_EPOCH.fetch_add(1, Ordering::Relaxed);

        if wait_until_backend_ready(epoch).is_err() {
            log_timeout_input(buf);
            self.crashed = true;
            return Ok(ExitKind::Timeout);
        }

        publish_input(epoch, buf);
        let wait_result = wait_until_complete(epoch);
        clear_input();

        if wait_result.is_err() {
            log_timeout_input(buf);
            self.crashed = true;
            return Ok(ExitKind::Timeout);
        }

        match take_assert_status() {
            ASSERT_RECOVERABLE => Ok(ExitKind::Crash),
            ASSERT_FATAL => {
                log_fatal_input(buf);
                self.crashed = true;
                Ok(ExitKind::Crash)
            }
            _ => Ok(ExitKind::Ok),
        }
    }
}

pub fn fuzzer_thread_main() {
    // Create an observation channel using the signals map
    let observer = unsafe { StdMapObserver::new("cvg", &mut CVG) };

    // Feedback to rate the interestingness of an input
    let mut feedback = MaxMapFeedback::new(&observer);

    let calibration_stage = CalibrationStage::new(&feedback);
    let stats_stage = AflStatsStage::builder()
        .map_feedback(&feedback)
        .build()
        .unwrap();

    // A feedback to choose if an input is a solution or not
    let mut objective = feedback_or_fast!(
        feedback_and_fast!(
            // Look for crashes.
            CrashFeedback::new(),
            // We `and` the MaxMapFeedback to only end up with crashes that trigger new coverage.
            // We use the _fast variant to make sure it's not evaluated every time, even if the crash didn't trigger..
            // We have to give this one a name since it differs from the first map.
            MaxMapFeedback::with_name("on_crash", &observer)
        ),
        TimeoutFeedback::new()
    );

    let corpus_path = PathBuf::from("fastdyn_work/corpus");
    let crashes_path = PathBuf::from("fastdyn_work/crashes");

    fs::create_dir_all(&corpus_path).expect("Couldn't create corpus directory");
    fs::create_dir_all(&crashes_path).expect("Couldn't create crashes directory");
    INTERESTING_TRACE_COUNT.store(
        next_interesting_trace_index(&corpus_path),
        Ordering::Relaxed,
    );

    let mut corpus = OnDiskCorpus::<BytesInput>::new(corpus_path.clone()).unwrap();
    //let mut corpus = InMemoryCorpus::new();
    let crash_corpus = OnDiskCorpus::<BytesInput>::new(crashes_path.clone()).unwrap();

    let mut initial_corpus_paths = std::fs::read_dir(&corpus_path)
        .unwrap()
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .collect::<Vec<_>>();
    initial_corpus_paths.sort();

    let mut pending_initial_traces = Vec::new();
    for path in initial_corpus_paths {
        if path.is_file() && path.extension().map_or(true, |ext| ext != "trace") {
            let bytes = std::fs::read(&path).unwrap();
            corpus.add(BytesInput::new(bytes.clone()).into()).unwrap();
            pending_initial_traces.push((path, BytesInput::new(bytes)));
        }
    }

    let mut state = StdState::new(
        StdRand::with_seed(current_nanos()),
        corpus,
        crash_corpus,
        &mut feedback,
        &mut objective,
    )
    .unwrap();

    // The Monitor trait defines how the fuzzer stats are displayed to the user.
    // Keep the human-readable monitor, and also expose structured snapshots for
    // external tooling without requiring it to parse stdout.
    #[cfg(not(feature = "tui"))]
    let mon = SimpleMonitor::new(|s| println!("{s}"));
    #[cfg(feature = "tui")]
    let mon = TuiMonitor::builder()
        .title("Baby Fuzzer")
        .enhanced_graphics(false)
        .build();

    let mut last_json_stats_write = None;
    let json_mon = OnDiskJsonMonitor::new("fastdyn_work/stats.jsonl", move |_| {
        let now = Instant::now();
        match last_json_stats_write {
            Some(last_write) if now.duration_since(last_write) < Duration::from_secs(10) => false,
            _ => {
                last_json_stats_write = Some(now);
                true
            }
        }
    });

    // The event manager handle the various events generated during the fuzzing loop
    // such as the notification of the addition of a new item to the corpus
    let mut mgr = SimpleEventManager::new((mon, json_mon));

    // A queue policy to get testcasess from the corpus
    let scheduler = QueueScheduler::new();

    // A fuzzer with feedbacks and a corpus scheduler
    #[cfg(not(feature = "bloom_input_filter"))]
    let mut fuzzer = StdFuzzer::new(scheduler, feedback, objective);
    #[cfg(feature = "bloom_input_filter")]
    //let filter = BloomInputFilter::new(10_000_000, 0.001);
    #[cfg(feature = "bloom_input_filter")]
    let mut fuzzer = StdFuzzerBuilder::new()
        //.input_filter(filter)
        .scheduler(scheduler)
        .feedback(feedback)
        .objective(objective)
        .build();

    // Create the executor for an in-process function with just one observer
    let executor = FastDynExecutor::new(&state, pending_initial_traces);
    let mut executor = WithObservers::new(executor, tuple_list!(observer));

    // If corpus is empty, generate random inputs
    if state.corpus().count() == 0 {
        let mut generator = RandPrintablesGenerator::with_min_size(
            NonZeroUsize::new(20).expect(""),
            NonZeroUsize::new(300).expect(""),
        );
        println!("Generating new inputs");
        state
            .generate_initial_inputs(&mut fuzzer, &mut executor, &mut generator, &mut mgr, 8)
            .expect("Failed to generate the initial corpus");
    }

    // Setup a mutational stage with a basic bytes mutator

    let havoc_mutator = HavocScheduledMutator::new(havoc_mutations());

    let mut stages = tuple_list!(
        calibration_stage,
        StdMutationalStage::new(havoc_mutator),
        ProgressStage::new(),
        stats_stage,
    );

    log_progress("fuzzer_started");

    loop {
        if STOP_FLAG.load(Ordering::SeqCst) {
            unsafe {
                fuzz_dump_bbl();
            }
            panic!("Fuzzer stopping as requested");
        }

        if let Err(err) = fuzzer.fuzz_one(&mut stages, &mut executor, &mut state, &mut mgr) {
            eprintln!("Fuzzing error: {:?}", err);
            break;
        }
    }
}

#[no_mangle]
pub extern "C" fn fuzz_libAFL_init() -> u32 {
    STOP_FLAG.store(false, Ordering::SeqCst);
    BACKEND_WAITING_EPOCH.store(0, Ordering::Release);
    FUZZ_CYCLES.store(0, Ordering::Release);
    EXECUTOR_RUNS.store(0, Ordering::Release);
    BACKEND_WAIT_CALLS.store(0, Ordering::Release);
    BACKEND_DELIVERIES.store(0, Ordering::Release);
    BACKEND_COMPLETIONS.store(0, Ordering::Release);
    BACKEND_WAITING_FOR_EPOCH.store(0, Ordering::Release);

    let handle = std::thread::spawn(move || {
        fuzzer_thread_main();
    });

    *FUZZ_THREAD.lock().unwrap() = Some(handle);

    return 1;
}
