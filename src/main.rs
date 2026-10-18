mod tools;

use reqwest::blocking::Client;
use serde_json::{json, Value};
use std::env;
use std::error::Error;
use std::path::PathBuf;
use std::time::Duration;

const MAX_ROUNDS: usize = 8;
const MAX_TOOL_CALLS: usize = 24;
const INSTRUCTIONS: &str = "You are a read-only assistant analyzing the project at the tool root. Use the available tools to inspect files before making claims about the project. Cite relevant file paths and line numbers in your answer. Do not claim that uninspected files say something. If a tool fails, explain the limitation.";

#[derive(Clone, Copy, PartialEq, Eq)]
enum Provider {
    DeepSeek,
    OpenAi,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("Error: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let (root, question) = parse_args()?;
    let root = root.canonicalize()?;
    if !root.is_dir() {
        return Err("project root must be a directory".into());
    }

    let provider = match env::var("AGENTTRY_PROVIDER").as_deref() {
        Ok("openai") => Provider::OpenAi,
        Ok("deepseek") | Err(_) => Provider::DeepSeek,
        Ok(_) => return Err("AGENTTRY_PROVIDER must be deepseek or openai".into()),
    };
    let (api_key, model, base_url) = match provider {
        Provider::DeepSeek => (
            env::var("DEEPSEEK_API_KEY").map_err(|_| "DEEPSEEK_API_KEY is not set")?,
            env::var("DEEPSEEK_MODEL").unwrap_or_else(|_| "deepseek-flash".into()),
            env::var("DEEPSEEK_BASE_URL").unwrap_or_else(|_| "https://api.deepseek.com".into()),
        ),
        Provider::OpenAi => (
            env::var("OPENAI_API_KEY").map_err(|_| "OPENAI_API_KEY is not set")?,
            env::var("OPENAI_MODEL").map_err(|_| "OPENAI_MODEL is not set")?,
            env::var("OPENAI_BASE_URL").unwrap_or_else(|_| "https://api.openai.com/v1".into()),
        ),
    };
    let client = Client::builder().timeout(Duration::from_secs(60)).build()?;

    let mut input = json!(question);
    let mut history = vec![json!({"role": "user", "content": question})];
    let mut previous_response_id: Option<String> = None;
    let mut tool_call_count = 0;

    for _ in 0..MAX_ROUNDS {
        let request = build_request(
            provider,
            &model,
            &input,
            &history,
            previous_response_id.as_deref(),
        );

        let response = client
            .post(format!("{}/responses", base_url.trim_end_matches('/')))
            .bearer_auth(&api_key)
            .json(&request)
            .send()?;
        let status = response.status();
        let body: Value = response.json()?;
        if !status.is_success() {
            return Err(format!("model API returned {status}: {body}").into());
        }
        if body["status"] != "completed" {
            return Err(format!("model response did not complete: {}", body["status"]).into());
        }

        if provider == Provider::OpenAi {
            previous_response_id = Some(
                body["id"]
                    .as_str()
                    .ok_or("model response did not include an id")?
                    .to_owned(),
            );
        }
        let output = body["output"]
            .as_array()
            .ok_or("model response did not include output")?;
        let mut tool_outputs = Vec::new();
        let mut answer = String::new();

        for item in output {
            match item["type"].as_str() {
                Some("function_call") => {
                    let call_id = item["call_id"]
                        .as_str()
                        .ok_or("function call is missing call_id")?;
                    let name = item["name"]
                        .as_str()
                        .ok_or("function call is missing name")?;
                    let result = if tool_call_count >= MAX_TOOL_CALLS {
                        "Tool error: tool call limit reached".to_owned()
                    } else {
                        match item["arguments"]
                            .as_str()
                            .ok_or("missing arguments")
                            .and_then(|raw| {
                                serde_json::from_str::<Value>(raw)
                                    .map_err(|_| "invalid JSON arguments")
                            }) {
                            Ok(arguments) => tools::execute(&root, name, &arguments),
                            Err(error) => format!("Tool error: {error}"),
                        }
                    };
                    tool_call_count += 1;
                    tool_outputs.push(json!({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result
                    }));
                }
                Some("message") => {
                    if let Some(content) = item["content"].as_array() {
                        for part in content {
                            if part["type"] == "output_text" {
                                if let Some(text) = part["text"].as_str() {
                                    answer.push_str(text);
                                }
                            }
                        }
                    }
                }
                _ => {}
            }
        }

        if tool_outputs.is_empty() {
            if answer.trim().is_empty() {
                return Err("model returned neither a tool call nor an answer".into());
            }
            println!("{answer}");
            return Ok(());
        }
        if provider == Provider::DeepSeek {
            history.extend(output.iter().cloned());
            history.extend(tool_outputs);
        } else {
            input = json!(tool_outputs);
        }
    }

    Err(format!("model exceeded the limit of {MAX_ROUNDS} rounds").into())
}

fn build_request(
    provider: Provider,
    model: &str,
    input: &Value,
    history: &[Value],
    previous_response_id: Option<&str>,
) -> Value {
    let mut request = json!({
        "model": model,
        "instructions": INSTRUCTIONS,
        "input": if provider == Provider::DeepSeek { json!(history) } else { input.clone() },
        "tools": tool_definitions()
    });
    if provider == Provider::OpenAi {
        request["store"] = json!(true);
        if let Some(response_id) = previous_response_id {
            request["previous_response_id"] = json!(response_id);
        }
    }
    request
}

fn parse_args() -> Result<(PathBuf, String), Box<dyn Error>> {
    let mut args = env::args().skip(1);
    let mut root = env::current_dir()?;
    let mut question = Vec::new();

    while let Some(argument) = args.next() {
        if argument == "--root" {
            root = PathBuf::from(args.next().ok_or("--root requires a path")?);
        } else if argument == "--help" {
            println!("Usage: agent_try [--root PATH] QUESTION\nSet DEEPSEEK_API_KEY first. Default model: deepseek-flash (DeepSeek V4.1 Flash).\nSet AGENTTRY_PROVIDER=openai to use OPENAI_API_KEY and OPENAI_MODEL instead.");
            std::process::exit(0);
        } else {
            question.push(argument);
        }
    }

    if question.is_empty() {
        return Err("provide a question; use --help for usage".into());
    }
    Ok((root, question.join(" ")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deepseek_replays_full_history_without_server_state() {
        let history = vec![
            json!({"role": "user", "content": "inspect the project"}),
            json!({"type": "reasoning", "content": "Need files"}),
            json!({"type": "function_call", "call_id": "call_1", "name": "list_files", "arguments": "{}"}),
            json!({"type": "function_call_output", "call_id": "call_1", "output": "README.md"}),
        ];
        let request = build_request(
            Provider::DeepSeek,
            "deepseek-flash",
            &Value::Null,
            &history,
            None,
        );
        assert_eq!(request["input"], json!(history));
        assert!(request.get("previous_response_id").is_none());
        assert!(request.get("store").is_none());
    }
}

fn tool_definitions() -> Value {
    json!([
        {
            "type": "function",
            "name": "list_files",
            "description": "List project files relative to the root, excluding Git internals and build output.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": false
            },
            "strict": true
        },
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a UTF-8 project file by its path relative to the root. Returns numbered lines.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative file path"}},
                "required": ["path"],
                "additionalProperties": false
            },
            "strict": true
        }
    ])
}
