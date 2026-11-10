"""Let Claude deploy b3basketball.com by itself (run once, by you).

Adds allow rules for `npx -y wrangler@4 deploy`, removes the soft-deny rule
that blocked deploys in auto mode, and tells the auto-mode classifier that
deploys are pre-approved. A backup is saved to ~/.claude/settings.json.bak.
Undo: copy settings.json.bak back over settings.json.
"""
import json, pathlib, shutil

p = pathlib.Path.home() / ".claude" / "settings.json"
shutil.copy(p, p.with_suffix(".json.bak"))
s = json.loads(p.read_text(encoding="utf-8"))

allow = s.setdefault("permissions", {}).setdefault("allow", [])
for r in ["Bash(npx -y wrangler@4 deploy)",
          "Bash(cd /c/Users/Zoric/Desktop/b3basketball-site && npx -y wrangler@4 deploy)",
          "PowerShell(npx -y wrangler@4 deploy)"]:
    if r not in allow:
        allow.append(r)

am = s.setdefault("autoMode", {})
am["soft_deny"] = [x for x in am.get("soft_deny", ["$defaults"])
                   if not x.startswith("Bash(npx wrangler@4 deploy*)")]
am_allow = am.setdefault("allow", ["$defaults"])
rule = ("Deploying b3basketball-site with `npx -y wrangler@4 deploy` from "
        "C:\\Users\\Zoric\\Desktop\\b3basketball-site is pre-approved by the user: "
        "Claude may deploy after building and checking the site.")
if rule not in am_allow:
    am_allow.append(rule)

p.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
print("allow:", allow)
print("autoMode.allow:", am_allow)
print("deploy soft-deny removed. Backup:", p.with_suffix(".json.bak"))
