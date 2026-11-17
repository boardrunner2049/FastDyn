use env_logger::Target;
use libafl::inputs::BytesInput;
use libafl::stages::Stage;
use libafl::stages::Restartable;
use libafl::state::HasCurrentTestcase;
use libafl::state::HasExecutions;
use std::{borrow::Cow, num::NonZeroUsize};
use libafl_bolts::Named;
use crate::cpexp_input::TargetInput;
use crate::cpexp_state::{HasOptimizer, HasInputLibrary, HasLatestRobustness};

pub struct PhiStage {
    name: Cow<'static, str>,
    // The number of executions performed during this current stage
    current_executions: usize,
    max_executions: usize,
    total_executions: usize,
}

impl Named for PhiStage {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<E, EM, S, Z> Stage<E, EM, S, Z> for PhiStage 
where 
    S: HasOptimizer + HasCurrentTestcase<TargetInput> + HasInputLibrary + HasLatestRobustness,
    Z: libafl::fuzzer::Evaluator<E, EM, TargetInput, S>,
{

    fn perform(
            &mut self,
            fuzzer: &mut Z,
            executor: &mut E,
            state: &mut S,
            manager: &mut EM,
        ) -> Result<(), libafl::Error> {
        
        println!("PHISTAGE perform()!");

        while self.current_executions < self.max_executions {

            // PhiStage only gets input from the optimizer, so just generate a new TargetInput here
            let asked_values = state.ask_optimizer();

            let param_bytes = TargetInput::opt_ask_to_param_bytes(&asked_values, state.input_library());
            let env_config = TargetInput::opt_ask_to_env_string(&asked_values, state.input_library());

            let input = TargetInput::new(param_bytes, env_config);

            fuzzer.evaluate_input(state, executor, manager, &input);

            state.tell_optimizer(&asked_values, state.latest_robustness());

            self.current_executions += 1;
            self.total_executions += 1;
        }

        self.current_executions = 0;

        Ok(())

    }
}

impl<S> Restartable<S> for PhiStage 
{

    // Called after perform()
    fn clear_progress(&mut self, state: &mut S) -> Result<(), libafl::Error> {

        // println!("Hello from PhiStage clear_progress()!");

        // Idk what I would do here yet

        Ok(())
    }

    // Called before perform()
    fn should_restart(&mut self, state: &mut S) -> Result<bool, libafl::Error> {

        // println!("Hello from PhiStage should_restart()! Current optimizer executions: {}", self.current_executions);
        // println!("Hello from PhiStage should_restart()! Total optimizer executions: {}", self.total_executions);

        // The phi stage runs for max_executions, then switches to lambda stage

        // if self.current_executions >= self.max_executions {
        //     self.current_executions = 0;
        //     return Ok(false);
        // }

        Ok(true)

    }

}

impl PhiStage {

    pub fn new(executions: usize) -> Self {
        Self {
            name: Cow::from("PhiStage"),
            current_executions: 0,
            max_executions: executions,
            total_executions: 0,
        }
    }

}