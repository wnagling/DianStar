"""Bounded semantic review; may restrict execution but never grants rights."""
import asyncio
import hashlib
import json
import re


RISK = re.compile(r"(?i)(?:\b(?:rm|rmdir|rd|del|erase|remove-item|clear-content|set-content|out-file|"
                  r"remove|unlink|rmtree|rename|move-item|mv|format|truncate|ri|delete|robocopy)\b|[>]|"
                  r"\bgit(?:\.exe)?\b\s+(?:clean|reset|restore|checkout)\b|"
                  r"\b(?:python\w*|node|ruby|perl|cmd|bash|sh|pwsh|powershell)(?:\.exe)?\b)")


class CommandAuditor:
    def __init__(self, reviewer, record, *, question, scanner, limit=8, timeout=10):
        self.reviewer, self.record = reviewer, record
        self.question, self.scanner = question, scanner
        self.limit, self.timeout, self.used = limit, timeout, 0

    async def __call__(self, name, args, plan):
        # A heuristic trigger is not a complete detector of script side effects.
        command = args.get("command") or json.dumps(args.get("argv", []), ensure_ascii=False)
        if not RISK.search(command):
            return
        binding = hashlib.sha256(json.dumps({"tool": name, "args": args}, sort_keys=True,
            ensure_ascii=False).encode()).hexdigest()
        event = {"event": "command_audit", "mode": "enforce", "tool": name,
                 "binding": binding, "affects_authorization": True}
        if self.used >= self.limit:
            event.update(status="budget_exhausted")
        else:
            self.used += 1  # Reserve before awaiting, including concurrent calls.
            payload = {"user_task": self.question, "tool": name,
                "repository": args["repository"], "command": command,
                "network": bool(args.get("network", False)),
                "execution_mode": plan.get("execution_mode", "direct"),
                "environment_names": sorted(args.get("environment", {})),
                "target_inventory": "unknown; no file content or deletion inventory supplied",
                "recovery": "shell changes have no automatic rollback"}
            encoded = json.dumps(payload, ensure_ascii=False)
            # Redact before truncation so a cut secret cannot escape detection.
            safe = self.scanner.redact(encoded, env_style=True)
            if safe != encoded or self.scanner.scan("command-audit.txt", encoded.encode()):
                event.update(status="sensitive_input_skipped")
            elif len(encoded) > 20000:
                event.update(status="input_too_large")
            else:
                try:
                    async with asyncio.timeout(self.timeout):
                        result = await self.reviewer(safe)
                    if (not isinstance(result, dict) or set(result) != {"decision", "reason"}
                            or result["decision"] not in {"allow", "deny", "confirm"}
                            or not isinstance(result["reason"], str) or not result["reason"].strip()
                            or len(result["reason"]) > 500):
                        raise ValueError("Invalid audit result")
                    event.update(status="reviewed", decision=result["decision"],
                        reason=self.scanner.redact(result["reason"], env_style=True))
                except Exception:
                    # Never log SDK errors, which may contain request material.
                    event.update(status="unavailable")
        try:
            await self.record(event)
        except Exception:
            # Observation logging never becomes an execution gate.
            # Cancellation still propagates; exception text stays private.
            pass
        return event
