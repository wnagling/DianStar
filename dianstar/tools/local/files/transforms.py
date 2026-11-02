"""Plan patch and move as ordinary, recoverable file transaction entries."""
import os

from .patches import apply_hunks, parse_patch, patch_paths
from .text import FileError, encode, load


def transform_paths(name, args):
    return [args["source"], args["destination"]] if name == "move" else patch_paths(parse_patch(args["patch"]))


def read_source(writer, args, relative):
    path = writer.context.path({"repository": args["repository"], "path": relative})
    snapshot = load(path)
    if snapshot is None:
        raise FileError("file_not_found", "补丁或移动的源文件不存在。")
    writer.context.security.run("before_read", [(relative, snapshot.data)])
    if writer.reads.get(str(path)) != snapshot:
        raise FileError("file_read_required", "补丁修改、删除和移动已有文件前，须先 read 当前版本。")
    return snapshot


def move_entries(writer, root, args, source, destination, snapshot, text):
    first = writer.context.path({"repository": args["repository"], "path": source})
    target = writer.context.path({"repository": args["repository"], "path": destination})
    if os.path.normcase(str(first)) == os.path.normcase(str(target)):
        raise FileError("file_move_same_path", "源和目标是同一路径；当前不支持仅改变大小写的重命名。")
    if load(target) is not None:
        raise FileError("file_destination_exists", "移动目标已存在，未覆盖；请先选择一个不存在的目标。")
    # Preserve the source's BOM and mode even though the destination is new.
    data = snapshot.data if text == snapshot.text else encode(text, snapshot.bom)
    created = writer.entry(root, "write", {"repository": args["repository"], "path": destination, "content": text}, exact_data=data)
    created["after"]["mode"] = snapshot.version[-1]
    created["moved_from"] = source
    deleted = writer.entry(root, "workspace_delete", {"repository": args["repository"], "path": source})
    deleted["moved_to"] = destination
    # Publish the destination first. If source removal fails, compensate the
    # destination; never delete the only original before its copy is durable.
    return [created, deleted]


def transform_entries(writer, root, name, args):
    if name == "move":
        snapshot = read_source(writer, args, args["source"])
        return move_entries(writer, root, args, args["source"], args["destination"], snapshot, snapshot.text)
    entries = []
    for item in parse_patch(args["patch"]):
        values = {"repository": args["repository"], "path": item.path}
        if item.operation == "add":
            if load(writer.context.path(values)) is not None:
                raise FileError("file_destination_exists", "补丁新增目标已存在，未覆盖。")
            entries.append(writer.entry(root, "write", {**values, "content": item.content}))
            continue
        snapshot = read_source(writer, args, item.path)
        if item.operation == "delete":
            entries.append(writer.entry(root, "workspace_delete", values))
            continue
        text = apply_hunks(snapshot, item.hunks) if item.hunks else snapshot.text
        if item.destination:
            entries.extend(move_entries(writer, root, args, item.path, item.destination, snapshot, text))
        else:
            entries.append(writer.entry(root, "write", {**values, "content": text}, exact_data=encode(text, snapshot.bom)))
    return entries
