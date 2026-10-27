"""Prove repository bounds for official GitHub search queries, without rewriting them."""
import re

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
TOKEN = re.compile(r'(?:[^\s()"\\]|"(?:\\.|[^"\\])*")+|[()]')


def search_repositories(arguments, allowed):
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 1024:
        raise ValueError("invalid query")
    tokens, end = [], 0
    for match in TOKEN.finditer(query):
        if query[end:match.start()].strip():
            raise ValueError("invalid search syntax")
        tokens.append(match.group())
        end = match.end()
    if query[end:].strip() or len(tokens) > 128:
        raise ValueError("invalid search syntax")
    position = 0

    def atom():
        nonlocal position
        if position >= len(tokens):
            raise ValueError("incomplete query")
        token = tokens[position]
        position += 1
        if token == "NOT":
            atom()
            return None
        if token == "(":
            value = expression()
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError("unclosed group")
            position += 1
            return value
        if token in {"AND", "OR", ")"}:
            raise ValueError("invalid operator")
        negative = token.startswith("-")
        qualifier = token[1:] if negative else token
        if qualifier.lower().startswith("repo:"):
            repository = qualifier[5:]
            if repository.startswith('"') and repository.endswith('"'):
                repository = repository[1:-1]
            if not REPOSITORY.fullmatch(repository):
                raise ValueError("invalid repository")
            repository = repository.lower()
            if not negative and repository not in allowed:
                raise ValueError("repository not allowed")
            return None if negative else {repository}
        return None

    def conjunction():
        nonlocal position
        value = atom()
        while position < len(tokens) and tokens[position] not in {"OR", ")"}:
            if tokens[position] == "AND":
                position += 1
            other = atom()
            value = other if value is None else value if other is None else value & other
        return value

    def expression():
        nonlocal position
        value = conjunction()
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            other = conjunction()
            value = None if value is None or other is None else value | other
        return value

    scoped = expression()
    if position != len(tokens):
        raise ValueError("invalid query")
    owner, repo = arguments.get("owner"), arguments.get("repo")
    if owner is not None or repo is not None:
        if not isinstance(owner, str) or not isinstance(repo, str):
            raise ValueError("incomplete repository")
        explicit = f"{owner}/{repo}".lower()
        if explicit not in allowed:
            raise ValueError("repository not allowed")
        # Official issue/PR search supports owner/repo. Without OR, appending
        # its repository qualifier cannot leave an unbounded Boolean branch.
        if scoped is None and "OR" not in tokens:
            scoped = {explicit}
        elif scoped != {explicit}:
            raise ValueError("conflicting repository scopes")
    if not scoped:
        raise ValueError("each OR branch needs an allowed repo qualifier")
    return scoped
