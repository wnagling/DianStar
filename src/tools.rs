use serde_json::Value;
use std::fs;
use std::path::{Component, Path, PathBuf};

const MAX_FILES: usize = 200;
const MAX_FILE_BYTES: u64 = 64 * 1024;

pub fn execute(root: &Path, name: &str, arguments: &Value) -> String {
    let result = match name {
        "list_files"
            if arguments
                .as_object()
                .is_some_and(|object| object.is_empty()) =>
        {
            list_files(root)
        }
        "list_files" => Err("list_files takes no arguments".into()),
        "read_file" => arguments["path"]
            .as_str()
            .ok_or_else(|| "path must be a string".to_owned())
            .and_then(|path| read_file(root, path)),
        _ => Err(format!("unknown tool: {name}")),
    };
    match result {
        Ok(output) => output,
        Err(error) => format!("Tool error: {error}"),
    }
}

fn list_files(root: &Path) -> Result<String, String> {
    let mut pending = vec![root.to_path_buf()];
    let mut files = Vec::new();

    while let Some(directory) = pending.pop() {
        let entries = fs::read_dir(&directory).map_err(|error| error.to_string())?;
        for entry in entries {
            let entry = entry.map_err(|error| error.to_string())?;
            let path = entry.path();
            let file_type = entry.file_type().map_err(|error| error.to_string())?;
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if name.starts_with('.') {
                continue;
            }
            if file_type.is_dir() {
                if name != "target" {
                    pending.push(path);
                }
            } else if file_type.is_file() {
                files.push(
                    path.strip_prefix(root)
                        .map_err(|error| error.to_string())?
                        .to_string_lossy()
                        .replace('\\', "/"),
                );
                if files.len() >= MAX_FILES {
                    files.sort();
                    return Ok(format!(
                        "{}\n[listing stopped at {MAX_FILES} files]",
                        files.join("\n")
                    ));
                }
            }
        }
    }

    files.sort();
    Ok(files.join("\n"))
}

fn read_file(root: &Path, relative_path: &str) -> Result<String, String> {
    let requested = PathBuf::from(relative_path);
    if requested.is_absolute() {
        return Err("path must be relative to the project root".into());
    }
    if requested.components().any(|component| {
        matches!(
            component,
            Component::ParentDir | Component::Prefix(_) | Component::RootDir
        ) || matches!(component, Component::Normal(name) if name.to_string_lossy().starts_with('.'))
    }) {
        return Err("path contains a restricted component".into());
    }
    let path = root
        .join(requested)
        .canonicalize()
        .map_err(|error| error.to_string())?;
    if !path.starts_with(root) || !path.is_file() {
        return Err("path must be a file inside the project root".into());
    }
    let size = fs::metadata(&path)
        .map_err(|error| error.to_string())?
        .len();
    if size > MAX_FILE_BYTES {
        return Err(format!("file exceeds the {MAX_FILE_BYTES} byte limit"));
    }
    let content = fs::read_to_string(path).map_err(|error| error.to_string())?;
    Ok(content
        .lines()
        .enumerate()
        .map(|(index, line)| format!("{}: {line}", index + 1))
        .collect::<Vec<_>>()
        .join("\n"))
}

#[cfg(test)]
mod tests {
    use super::read_file;
    use std::fs;

    #[test]
    fn rejects_parent_traversal() {
        let directory = std::env::temp_dir().join(format!("agent_try_test_{}", std::process::id()));
        fs::create_dir_all(&directory).unwrap();
        let root = directory.canonicalize().unwrap();
        fs::write(root.join("inside.txt"), "ok").unwrap();
        assert!(read_file(&root, "inside.txt").is_ok());
        assert!(read_file(&root, "../outside.txt").is_err());
        assert!(read_file(&root, ".env").is_err());
        fs::remove_dir_all(directory).unwrap();
    }
}
