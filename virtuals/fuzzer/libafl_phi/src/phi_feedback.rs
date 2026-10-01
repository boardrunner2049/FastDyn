use libafl::feedbacks::{Feedback, StateInitializer};
use libafl::observers::ObserversTuple;
use libafl_bolts::Named;
use libafl::Error;
use libafl_bolts::tuples::{Handle, MatchNameRef};
use std::borrow::Cow;

use crate::PhysicalObserver;

pub struct PhysicalFeedback {
    name: Cow<'static, str>,
    physical_observer_handle: Handle<PhysicalObserver>,
    minimum_robustness_vec: Vec<f64>, // Minimum robustness values observed for each formula
}

impl Named for PhysicalFeedback {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<S> StateInitializer<S> for PhysicalFeedback {
    fn init_state(&mut self, _state: &mut S) -> Result<(), libafl::Error> {
        Ok(())
    }
}

impl PhysicalFeedback {

    pub fn new() -> Self {

        let feedback = Self {
            name: Cow::from("PhysicalFeedback"),
            physical_observer_handle: Handle::new(Cow::from("PhysicalObserver")),
            minimum_robustness_vec: Vec::new(),
        };
        println!("name = {}", feedback.name);

        match &feedback.name {
            Cow::Borrowed(name) => println!("Feedback name (borrowed): {}", name),
            Cow::Owned(name) => println!("Feedback name (owned): {}", name),
        }

        feedback

    }

}

impl<EM, I, OT, S> Feedback<EM, I, OT, S> for PhysicalFeedback
where 
    OT: ObserversTuple<I, S>,
{
    fn is_interesting(
            &mut self,
            _state: &mut S,
            _manager: &mut EM,
            _input: &I,
            observers: &OT,
            _exit_kind: &libafl::executors::ExitKind,
        ) -> Result<bool, Error> {

        // println!("Hello from PhysicalFeedback is_interesting!");

        // Get the latest robustness vector from the PhysicalObserver
        let physical_observer = observers.get(&self.physical_observer_handle).unwrap();
        let robustness_vec: &Vec<f64> = physical_observer.get_robustness_vec();

        // Case 1: First observation, initialize minimum_robustness_vec
        if self.minimum_robustness_vec.is_empty() {
            self.minimum_robustness_vec = robustness_vec.clone();
            return Ok(true);
        }

        // Case 2: Subsequent observation, update minimum robustness values
        let mut is_interesting = false;
        for (i, &robustness) in robustness_vec.iter().enumerate() {
            if robustness < self.minimum_robustness_vec[i] {
                self.minimum_robustness_vec[i] = robustness;
                is_interesting = true;
            }
        }

        Ok(is_interesting)
    }
}