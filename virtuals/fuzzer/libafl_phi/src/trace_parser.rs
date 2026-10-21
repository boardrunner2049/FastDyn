use std::collections::BTreeMap;
use std::error::Error;
use std::io::{Error as IoError, ErrorKind, Read};
use std::path::Path;

const TRACE_FIELDS: [&str; 16] = [
    "time",
    "x",
    "y",
    "z",
    "roll",
    "pitch",
    "yaw",
    "lin_velo_x",
    "lin_velo_y",
    "lin_velo_z",
    "ang_velo_x",
    "ang_velo_y",
    "ang_velo_z",
    "lin_accel_x",
    "lin_accel_y",
    "lin_accel_z",
];

/// Summary statistics for one numeric field in a trace.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TraceStats {
    pub min: f64,
    pub max: f64,
    pub average: f64,
    pub samples: usize,
}

#[derive(Debug)]
struct Accumulator {
    min: f64,
    max: f64,
    average: f64,
    samples: usize,
}

impl Accumulator {
    fn new(value: f64) -> Self {
        Self {
            min: value,
            max: value,
            average: value,
            samples: 1,
        }
    }

    fn add(&mut self, value: f64) {
        self.samples += 1;
        self.min = self.min.min(value);
        self.max = self.max.max(value);
        self.average += (value - self.average) / self.samples as f64;
    }

    fn into_stats(self) -> TraceStats {
        TraceStats {
            min: self.min,
            max: self.max,
            average: self.average,
            samples: self.samples,
        }
    }
}

/// Read a headerless trace-recorder CSV and summarize each recorded field.
///
/// The trace recorder's column order is time, position, attitude, linear velocity,
/// angular velocity, and linear acceleration.
pub fn parse_trace_log(
    path: impl AsRef<Path>,
) -> Result<BTreeMap<String, TraceStats>, Box<dyn Error>> {
    summarize_reader(std::fs::File::open(path)?)
}

fn summarize_reader<R: Read>(reader: R) -> Result<BTreeMap<String, TraceStats>, Box<dyn Error>> {
    let mut csv_reader = csv::ReaderBuilder::new()
        .has_headers(true)
        .from_reader(reader);
    let headers = csv_reader.headers()?.clone();
    let mut accumulators: Vec<Option<Accumulator>> =
        (0..headers.len()).map(|_| None).collect();
    let mut rows = 0;

    for (row_index, result) in csv_reader.records().enumerate() {
        let record = result?;
        if record.len() != headers.len() {
            return Err(IoError::new(
                ErrorKind::InvalidData,
                format!(
                    "trace row {} has {} columns; expected {}",
                    row_index + 1,
                    record.len(),
                    headers.len()
                ),
            )
            .into());
        }

        for (column_index, field) in headers.iter().enumerate() {
            let value = record[column_index].parse::<f64>().map_err(|error| {
                IoError::new(
                    ErrorKind::InvalidData,
                    format!(
                        "invalid value for '{}' at trace row {}: {}",
                        field,
                        row_index + 1,
                        error
                    ),
                )
            })?;
            if !value.is_finite() {
                return Err(IoError::new(
                    ErrorKind::InvalidData,
                    format!(
                        "non-finite value for '{}' at trace row {}",
                        field,
                        row_index + 1
                    ),
                )
                .into());
            }

            match &mut accumulators[column_index] {
                Some(accumulator) => accumulator.add(value),
                None => accumulators[column_index] = Some(Accumulator::new(value)),
            }
        }
        rows += 1;
    }

    if rows == 0 {
        return Err(IoError::new(ErrorKind::InvalidData, "trace log contains no data rows").into());
    }

    Ok(headers
        .iter()
        .zip(accumulators)
        .map(|(field, accumulator)| {
            (
                (*field).to_string(),
                accumulator
                    .expect("non-empty trace rows initialize every field")
                    .into_stats(),
            )
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::{summarize_reader, TraceStats};
    use std::io::Cursor;

    #[test]
    fn computes_statistics_for_each_trace_field() {
        let trace = concat!(
            "1,10,20,30,0,0,0,1,2,3,4,5,6,7,8,9\n",
            "3,30,40,50,2,4,6,3,4,5,6,7,8,9,10,11\n"
        );

        let stats = summarize_reader(Cursor::new(trace)).unwrap();

        assert_eq!(
            stats["time"],
            TraceStats {
                min: 1.0,
                max: 3.0,
                average: 2.0,
                samples: 2,
            }
        );
        assert_eq!(
            stats["x"],
            TraceStats {
                min: 10.0,
                max: 30.0,
                average: 20.0,
                samples: 2,
            }
        );
        assert_eq!(stats["lin_accel_z"].average, 10.0);
        assert_eq!(stats.len(), 16);
    }

    #[test]
    fn rejects_rows_with_the_wrong_number_of_columns() {
        let error = summarize_reader(Cursor::new("1,2,3\n")).unwrap_err();

        assert!(error.to_string().contains("expected 16"));
    }

    #[test]
    fn rejects_empty_trace_logs() {
        let error = summarize_reader(Cursor::new("")).unwrap_err();

        assert!(error.to_string().contains("no data rows"));
    }
}
