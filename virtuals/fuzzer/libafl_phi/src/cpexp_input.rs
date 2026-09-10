use env_logger::Target;
use libafl::inputs::{BytesInput, Input, ResizableMutator, ValueInput};
use libafl::inputs::HasMutatorBytes;
use libafl_bolts::HasLen;
use core::hash;
use std::{collections::HashMap, env, fmt::format, hash::{DefaultHasher, Hash, Hasher}, ptr::hash};
use serde::{Deserialize, Serialize};
use std::fs;
use scirs2_core::Array1;

/*
    ParamInput, EnvInput, and CPExpInput are NOT meant to be used by LibAFL.
    A CPExpInput object will be stored inside of the LibAFL state object, and
    will primarily be used to translate optimizer outputs into the actual
    parameter/environmental values.
*/

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct ParamInput {
    name: String,

    // All Ardu_X parameters are floats in a range or categorical values
    is_float: bool,

    // If the parameter is float, specify the range and increment directly
    range: (f64, f64),
    increment: f64,

    // If the parameter is categorical (is_float == false), specify the vector of options
    // Then, we'll use the length of this vector to determine the range
    options: Vec<f64>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct EnvInput {
    name: String,

    // "Slider" to adjust environment inputs
    range: (f64, f64),

    // Some environments inputs are categorical, e.g. terrain
    // In this case, range represents the indices of the categorical options
    // that will be truncated to integers
    categorical: bool,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct CPExpInput {
    param_input: Vec<ParamInput>,
    env_input: Vec<EnvInput>,
}

impl EnvInput {

    pub fn new(name: &str, range: (f64, f64), categorical: bool) -> Self {

        Self {
            name: String::from(name),
            range: (range.0, range.1),
            categorical,
        }

    }

    pub fn get_name(&self) -> &String {
        &self.name
    }

    pub fn get_range(&self) -> &(f64, f64) {
        &self.range
    }

    pub fn is_categorical(&self) -> bool {
        self.categorical
    }

}

impl ParamInput {

    pub fn new_float(name: &str, range: (f64, f64), increment: f64) -> Self {

        Self {
            name: String::from(name),
            is_float: true,
            range: (range.0, range.1),
            increment,
            options: Vec::new(),
        }

    }

    pub fn new_categorical(name: &str, options: Vec<f64>) -> Self {

        Self {
            name: String::from(name),
            is_float: false,
            range: (0.0, options.len() as f64),
            increment: 0.0,
            options,
        }

    }

    pub fn get_name(&self) -> &String {
        &self.name
    }

    pub fn is_float(&self) -> bool {
        self.is_float
    }

    pub fn get_range(&self) -> &(f64, f64) {
        &self.range
    }

    pub fn get_increment(&self) -> &f64 {
        &self.increment
    }

    pub fn get_options(&self) -> &Vec<f64> {
        &self.options
    }

}

impl CPExpInput {

    pub fn new(param_input: Vec<ParamInput>, env_input: Vec<EnvInput>) -> Self {

        Self {
            param_input,
            env_input,
        }

    }

    pub fn get_param_input(&self) -> &Vec<ParamInput> {
        &self.param_input
    }

    pub fn get_env_input(&self) -> &Vec<EnvInput> {
        &self.env_input
    }

}

/*
    TargetInput is the actual input type that will be passed to the harness and
    live in the corpus. It represents the parameter inputs as raw bytes and 
    environmental inputs as a String of comma-separated f64 values for a single execution. 
*/

#[derive(Serialize, Deserialize, Debug, Clone, Hash)]
pub struct TargetInput {
    // Parameter inputs need to be raw bytes to be mutated by LibAFL
    param_bytes: BytesInput,
    // Need to use a String because f64's are not hashable
    env_config: String,
}

impl Input for TargetInput {

    fn from_file<P>(path: P) -> Result<Self, libafl::Error>
    where
        P: AsRef<std::path::Path>, 
    {
        let mut file = std::fs::File::open(path)?;
        let mut content = String::new();
        std::io::Read::read_to_string(&mut file, &mut content)?;
        
        let input = serde_json::from_str(&content)
            .map_err(|e| libafl::Error::serialize(e.to_string()))?;
        Ok(input)   
    }

    fn to_file<P>(&self, path: P) -> Result<(), libafl::Error>
    where
        P: AsRef<std::path::Path>, 
    {
        let json = serde_json::to_string(self)
            .map_err(|e| libafl::Error::serialize(e.to_string()))?;
        std::fs::write(path, json)?;
        Ok(())
    } 

    fn generate_name(&self, _id: Option<libafl::corpus::CorpusId>) -> String {
        let execution = env::var("EXECUTION").unwrap_or_else(|_| "unknown".to_string());
        let mut hasher = DefaultHasher::new();
        self.hash(&mut hasher);
        let hash = hasher.finish();
        format!("{}_{}", execution, hash)
    }

}

impl HasLen for TargetInput {
    fn len(&self) -> usize {
        self.param_bytes.len() + self.env_config.len()
    }
}

impl HasMutatorBytes for TargetInput {
    fn mutator_bytes(&self) -> &[u8] {
        self.get_param_bytes().as_ref().as_slice()
    }
    fn mutator_bytes_mut(&mut self) -> &mut [u8] {
        self.get_param_bytes_mut().as_mut().as_mut_slice()
    }
}

impl ResizableMutator<u8> for TargetInput {

    fn resize(&mut self, new_len: usize, value: u8) {
        self.get_param_bytes_mut().resize(new_len, value);
    }

    fn extend<'a, I: IntoIterator<Item = &'a u8>>(&mut self, iter: I)
    {
        self.get_param_bytes_mut().extend(iter);
                
    }

    fn splice<R, I>(&mut self, range: R, replace_with: I) -> std::vec::Splice<'_, I::IntoIter>
        where
            R: std::ops::RangeBounds<usize>,
            I: IntoIterator<Item = u8> {
        self.get_param_bytes_mut().splice(range, replace_with)
    }

    fn drain<R>(&mut self, range: R) -> std::vec::Drain<'_, u8>
        where
            R: std::ops::RangeBounds<usize> {
        self.get_param_bytes_mut().drain(range)
    }

}

impl TargetInput {

    pub fn new(param_bytes: BytesInput, env_config: String) -> Self {

        Self {
            param_bytes,
            env_config,
        }

    }

    pub fn f32_vec_to_bytes(input_vec: &Vec<f32>) -> BytesInput {
        
        let mut bytes_vec: Vec<u8> = Vec::new();
        for val in input_vec.iter() {
            let value_bytes = val.to_le_bytes();
            bytes_vec.extend_from_slice(&value_bytes);
        }

        BytesInput::new(bytes_vec)

    }

    pub fn bytes_to_f32_vec(input_bytes: &BytesInput) -> Vec<f32> {
        
        let bytes = input_bytes.as_ref();

        // TODO: What happens when the bytes length isn't equal to num_params * 4?
        // Ideas (though I make some assumptions about MAVLINK behavior):
        //   len < 4: Default parameter values
        //   len > num_params * 4: truncate (assuming MAVLINK doesn't accept extra bytes)
        //   len < num_params * 4: floor to nearest multiple of 4, convert those bytes

        let mut float_vec: Vec<f32> = Vec::new();

        for chunk in bytes.chunks_exact(4) {
            let array: [u8; 4] = chunk.try_into().expect("Slice with incorrect length");
            let value = f32::from_le_bytes(array);
            float_vec.push(value);
        }

        float_vec

    }

    pub fn opt_ask_to_param_bytes(ask_array: &Array1<f64>, input_library: &CPExpInput) -> BytesInput {
        
        // First, extract the parameter values from ask_array
        let ask_vec= ask_array.to_vec();
        let param_info_vec = input_library.get_param_input();
        let asked_param_values = &ask_vec[0..param_info_vec.len()];
        // println!("Asked parameter values from optimizer (pre-processing): {:?}", asked_param_values);

        // Process the proposed parameter values before byte conversion
        let mut processed_param_values: Vec<f32> = Vec::new();
        for (i, param_data) in param_info_vec.iter().enumerate() {
            let mut proposed_value = asked_param_values[i].clone();

            if param_data.is_float {
                // Clamp the value to the specified range (this optimizer may go out of bounds)
                let range = &param_data.range;
                proposed_value = proposed_value.clamp(range.0, range.1);

                // Round to nearest increment
                let increment = &param_data.increment;
                proposed_value = (proposed_value / increment).round() * increment;

            } else {
                // Categorical parameter
                let options = &param_data.options;

                // First, clamp the proposed value to (0, options.len())
                proposed_value = proposed_value.clamp(0.0, (options.len()) as f64);

                // Truncate to integer index
                let mut index = proposed_value.trunc() as usize;

                // Make sure index is within bounds
                if index >= options.len() {
                    index = options.len() - 1;
                }

                // Map to the actual categorical value
                proposed_value = options[index];

            }

            processed_param_values.push(proposed_value as f32);
        }

        Self::f32_vec_to_bytes(&processed_param_values)

    }

    pub fn opt_ask_to_env_string(ask_array: &Array1<f64>, input_library: &CPExpInput) -> String {

        // First, extract the environment values from ask_array
        let ask_vec= ask_array.to_vec();
        let env_info_vec = input_library.get_env_input();
        let asked_env_values = &ask_vec[ask_vec.len() - env_info_vec.len()..];
        // println!("Asked environment values from optimizer (pre-processing): {:?}", asked_env_values);

        // Make a comma-separated string of the environment values
        let mut env_string = String::new();
        for (i, env_data) in env_info_vec.iter().enumerate() {

            let mut proposed_value = asked_env_values[i].clone();

            // Clamp the value to the specified range (this optimizer may go out of bounds)
            proposed_value = proposed_value.clamp(env_data.range.0, env_data.range.1);

            // If categorical, truncate it
            if env_data.categorical {
                proposed_value = proposed_value.trunc();
            }

            // Finally, append to the env_string
            env_string.push_str(format!("{}:{},", env_data.name, proposed_value.to_string()).as_str());

        }

        env_string

    }

    pub fn get_param_bytes(&self) -> &BytesInput {
        &self.param_bytes
    }

    pub fn get_param_bytes_mut(&mut self) -> &mut BytesInput {
        &mut self.param_bytes
    }

    pub fn get_env_config(&self) -> &String {
        &self.env_config
    }

}

pub trait HasParamBytes {
    fn param_bytes(&self) -> &BytesInput;
    fn param_bytes_mut(&mut self) -> &mut BytesInput;
}

impl HasParamBytes for TargetInput {
    fn param_bytes(&self) -> &BytesInput {
        &self.param_bytes
    }
    fn param_bytes_mut(&mut self) -> &mut BytesInput {
        &mut self.param_bytes
    }
}

pub trait HasEnvConfig {
    fn env_config(&self) -> &String;
    fn env_config_mut(&mut self) -> &mut String;
    fn set_env_config(&mut self, new_config: String);
}

impl HasEnvConfig for TargetInput {
    fn env_config(&self) -> &String {
        &self.env_config
    }
    fn env_config_mut(&mut self) -> &mut String {
        &mut self.env_config
    }
    fn set_env_config(&mut self, new_config: String) {
        self.env_config = new_config;
    }
}
