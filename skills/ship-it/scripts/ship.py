#!/usr/bin/env python3
"""Save a site to GitHub and publish it on Vercel. Everything that creates or publishes is a dry run first.

  python3 scripts/ship.py doctor                                  # is git / gh / vercel installed and logged in?
  python3 scripts/ship.py repo my-site --dir .                    # dry run: prints the plan, changes nothing
  python3 scripts/ship.py repo my-site --create --confirmed       # init, commit, create the private repo, push
  python3 scripts/ship.py deploy --dir dist                       # dry run: prints the vercel command and project name
  python3 scripts/ship.py deploy --dir dist --prod --confirmed    # publish to production
  python3 scripts/ship.py domain-help mysite.com                  # prints the DNS records to add, changes nothing

Safety rules built in:
  * repo and deploy only print what they would run until you add --confirmed. Add it only after the owner said yes.
  * repo scans the files a commit would contain for secrets and refuses if it finds any.
  * deploy runs check_site.py first and refuses on errors (--force overrides everything except secrets).
  * deploy links the folder to a Vercel project named after the site's folder (or --project NAME), never after "dist",
    so one site cannot silently replace another. A folder name that is too generic needs --project.
  * The only programs ever started are git, gh and vercel, with fixed argument lists and never a shell.

Exit codes: 0 ok (a dry run that could run for real counts as ok), 1 refused or failed or not ready, 2 usage error.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import check_site  # noqa: E402

ALLOWED_TOOLS = ("git", "gh", "vercel")
SAFE_GITIGNORE = [".env", ".env.*", "!.env.example", "*.pem", "*.key", ".vercel/", "node_modules/", ".next/", ".DS_Store",
                  "Thumbs.db", "*.log", "__pycache__/", ".venv/", "venv/", ".cache/", ".turbo/"]
RunResult = collections.namedtuple("RunResult", "code out err")
DNS_A = "76.76.21.21"
DNS_CNAME = "cname.vercel-dns.com"
SECOND_LEVEL = frozenset(
    "co.uk org.uk ac.uk gov.uk me.uk ltd.uk plc.uk com.au net.au org.au edu.au gov.au co.nz org.nz net.nz co.za com.br "
    "com.mx co.jp ne.jp or.jp co.in net.in org.in com.sg com.hk com.tr com.ar co.kr com.cn com.tw com.my co.id com.ph "
    "com.vn co.il com.ua com.pl com.pt com.es com.co co.ke co.th".split())


class UsageError(Exception):
    """A user mistake: reported as one line, exit code 2."""


# ---- Running the three allowed tools ----

def have(tool):
    return shutil.which(tool)


def run(cmd, cwd=None, timeout=120):
    """Run git, gh or vercel with a fixed argument list. Never a shell, never any other program."""
    if not cmd or cmd[0] not in ALLOWED_TOOLS or not all(isinstance(c, str) for c in cmd):
        raise UsageError("internal error: refused to start %r" % (cmd[:1],))
    exe = shutil.which(cmd[0])
    if not exe:
        return RunResult(127, "", "%s: command not found" % cmd[0])
    env = dict((k, v) for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"))
    try:
        p = subprocess.run([exe] + list(cmd[1:]), cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return RunResult(124, "", "%s timed out after %d seconds" % (cmd[0], timeout))
    except OSError as ex:
        return RunResult(126, "", "%s could not start: %s" % (cmd[0], ex))
    return RunResult(p.returncode, p.stdout or "", p.stderr or "")


def show(cmd):
    return " ".join(shlex.quote(c) for c in cmd)


def first_line(text):
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:80]
    return ""


def tail(text, n=12):
    lines = [l.rstrip() for l in text.strip().splitlines() if l.strip()]
    return "\n".join("    " + l[:200] for l in lines[-n:])


def emit(data, as_json, lines):
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print("\n".join(lines))


def platform_name():
    p = sys.platform
    return "macOS" if p == "darwin" else "Windows" if p.startswith("win") else "Linux"


# ---- doctor ----

def install_hint(tool):
    plat = sys.platform
    if tool == "git":
        return "xcode-select --install" if plat == "darwin" else "winget install --id Git.Git -e" if plat.startswith("win") \
            else "sudo apt install git"
    if tool == "gh":
        return "brew install gh" if plat == "darwin" else "winget install --id GitHub.cli -e" if plat.startswith("win") \
            else "sudo apt install gh   (or see https://cli.github.com)"
    if have("npm"):
        return "npm install -g vercel"
    node = "brew install node" if plat == "darwin" else "winget install OpenJS.NodeJS.LTS" if plat.startswith("win") \
        else "sudo apt install nodejs npm"
    return "%s   (Node.js first), then: npm install -g vercel" % node


def looks_like_sandbox():
    return os.path.isdir("/mnt/user-data") or os.path.isdir("/mnt/skills")


def gather_status():
    tools = collections.OrderedDict()
    g = {"installed": bool(have("git")), "ready": False, "fix": []}
    if g["installed"]:
        g["version"] = first_line(run(["git", "--version"]).out)
        name = run(["git", "config", "user.name"]).out.strip()
        email = run(["git", "config", "user.email"]).out.strip()
        g["identity"] = {"name": name, "email": email}
        g["ready"] = bool(name and email)
        if not g["ready"]:
            g["problem"] = "git does not know your name and email yet (needed for commits)"
            g["fix"] = ['git config --global user.name "Your Name"', 'git config --global user.email "you@yourmail.com"']
    else:
        g["problem"] = "git is not installed"
        g["fix"] = [install_hint("git")]
    tools["git"] = g

    h = {"installed": bool(have("gh")), "ready": False, "fix": []}
    if h["installed"]:
        h["version"] = first_line(run(["gh", "--version"]).out)
        r = run(["gh", "auth", "status"], timeout=45)
        text = r.out + "\n" + r.err
        h["logged_in"] = r.code == 0
        m = re.search(r"Logged in to \S+ (?:account|as) (\S+)", text)
        h["user"] = m.group(1) if m else None
        sm = re.search(r"Token scopes?:\s*(.+)", text)
        h["scopes"] = re.findall(r"'([^']+)'", sm.group(1)) if sm else []
        h["ready"] = h["logged_in"]
        if not h["ready"]:
            h["problem"] = "gh is not logged in to GitHub"
            h["fix"] = ["gh auth login"]
    else:
        h["problem"] = "gh (the GitHub command line) is not installed"
        h["fix"] = [install_hint("gh"), "gh auth login"]
    tools["gh"] = h

    v = {"installed": bool(have("vercel")), "ready": False, "fix": []}
    if v["installed"]:
        v["version"] = first_line(run(["vercel", "--version"], timeout=60).out)
        r = run(["vercel", "whoami"], timeout=60)
        v["logged_in"] = r.code == 0
        v["user"] = first_line(r.out) if r.code == 0 else None
        v["ready"] = v["logged_in"]
        if not v["ready"]:
            v["problem"] = "vercel is not logged in"
            v["fix"] = ["vercel login"]
    else:
        v["problem"] = "vercel (the Vercel command line) is not installed"
        v["fix"] = [install_hint("vercel"), "vercel login"]
    tools["vercel"] = v
    return tools


def cmd_doctor(a):
    tools = gather_status()
    ready = all(t["ready"] for t in tools.values())
    none_found = not any(t["installed"] for t in tools.values())
    sandbox = looks_like_sandbox()
    data = {"ready": ready, "platform": platform_name(), "tools": tools, "sandbox_hint": sandbox or none_found}
    lines = ["Ship-it doctor (%s)" % platform_name()]
    for name, t in tools.items():
        if t["ready"]:
            if name == "git":
                who = t["identity"]
                detail = "%s, %s <%s>" % (t.get("version", ""), who["name"], who["email"])
            else:
                detail = "%s, logged in as %s" % (t.get("version", ""), t.get("user") or "?")
            lines.append("  %-7s OK       %s" % (name, detail))
        else:
            lines.append("  %-7s MISSING  %s" % (name, t["problem"]))
            for fix in t["fix"]:
                lines.append("          fix:     %s" % fix)
    lines.append("")
    lines.append("Ready to publish: %s" % ("yes" if ready else "no. Do the fixes above, then run doctor again."))
    if none_found or sandbox:
        lines += ["", "Note: %s If this is plain claude.ai chat, a sandbox or any place" %
                  ("none of git, gh or vercel could be found here." if none_found else "this looks like a sandbox."),
                  "without a real terminal, nothing can be installed or published from here. Connect the GitHub and Vercel",
                  "connectors in Claude settings, or use Claude Code or the Claude desktop app on your own computer."]
    emit(data, a.json, lines)
    return 0 if ready else 1


# ---- repo ----

def check_repo_name(name):
    ok = re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9-]{0,38}/)?[A-Za-z0-9_][A-Za-z0-9._-]{0,99}", name or "")
    if not ok or name.endswith(".git") or name.split("/")[-1] in (".", ".."):
        raise UsageError("'%s' is not a valid repository name (letters, digits, - _ . only, no spaces; OWNER/NAME is allowed)" % name)
    return name


def project_dir(path):
    if path.startswith("-"):
        raise UsageError("--dir must not start with '-'")
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(p):
        raise UsageError("no such folder: %s" % path)
    home = os.path.abspath(os.path.expanduser("~"))
    if p == home or os.path.dirname(p) == p:
        raise UsageError("refusing to use %s as a project folder; pick the folder of this one site" % p)
    return p


def repo_has_commits(proj):
    head = os.path.join(proj, ".git", "HEAD")
    try:
        with open(head, "r", encoding="utf-8", errors="replace") as fh:
            ref = fh.read().strip()
    except OSError:
        return False
    if not ref.startswith("ref:"):
        return True
    ref = ref[4:].strip()
    if os.path.isfile(os.path.join(proj, ".git", *ref.split("/"))):
        return True
    try:
        with open(os.path.join(proj, ".git", "packed-refs"), "r", encoding="utf-8", errors="replace") as fh:
            return any(line.strip().endswith(" " + ref) for line in fh)
    except OSError:
        return False


def has_origin(proj):
    try:
        with open(os.path.join(proj, ".git", "config"), "r", encoding="utf-8", errors="replace") as fh:
            return re.search(r'^\s*\[remote "origin"\]', fh.read(), re.M) is not None
    except OSError:
        return False


def missing_gitignore_lines(proj):
    try:
        with open(os.path.join(proj, ".gitignore"), "r", encoding="utf-8", errors="replace") as fh:
            have_lines = set(l.strip() for l in fh.read().splitlines())
    except OSError:
        have_lines = set()
    return [l for l in SAFE_GITIGNORE if l not in have_lines]


def write_gitignore(proj, lines):
    path = os.path.join(proj, ".gitignore")
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            existing = fh.read()
    except OSError:
        existing = ""
    sep = "" if not existing else ("" if existing.endswith("\n") else "\n") + "\n"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(existing + sep + "# Added by ship-it: keep secrets and junk out of git\n" + "\n".join(lines) + "\n")


def finding_lines(findings):
    out, seen = [], set()
    for f in findings:
        loc = f["file"] or ""
        if f["line"]:
            loc += ":%d" % f["line"]
        out.append("    %s  %s" % (loc, f["message"]))
        if f["fix"] not in seen:
            seen.add(f["fix"])
            out.append("        fix: %s" % f["fix"])
    return out


def repo_hint(text):
    low = text.lower()
    if "unknown switch" in low:
        return "Your git is older than 2.28. Update it (on a Mac: xcode-select --install), then run again."
    if "already exists" in low:
        return "That repository name is taken on your account. Pick another name and run again."
    if "workflow" in low and "scope" in low:
        return "gh needs one more permission: gh auth refresh -h github.com -s workflow"
    if "nothing to commit" in low:
        return "Every file is ignored by .gitignore, so there is nothing to save. Check the folder."
    if "could not resolve host" in low or "unable to access" in low or "network" in low:
        return "The network looks unreachable. Check the connection and run again."
    return ""


def cmd_repo(a):
    name = check_repo_name(a.name)
    proj = project_dir(a.dir)
    vis = "public" if a.public else "private"
    dry = not a.confirmed
    blocked, notes, plan = [], [], []
    is_repo = os.path.isdir(os.path.join(proj, ".git"))
    committed = is_repo and repo_has_commits(proj)
    new_lines = missing_gitignore_lines(proj)

    # 1. the secrets scan runs first and refuses before anything is touched
    rep = check_site.Report()
    check_site.scan_project(rep, proj, extra_ignore=new_lines)
    rep.finish()
    errors = [f for f in rep.findings if f["severity"] == "error"]
    if errors:
        blocked.append({"why": "secrets found in files that would be committed (%d)" % len(errors), "detail": finding_lines(errors)})
    if rep.stats.get("project_truncated"):
        blocked.append({"why": "more than 6000 files: this does not look like one project folder", "detail": []})
    if rep.stats.get("project_files", 0) == 0 and not rep.stats.get("project_truncated"):
        blocked.append({"why": "the folder has no files to save", "detail": []})
    notes += ["%s" % f["message"] for f in rep.findings if f["severity"] == "info"]
    notes += ["%s (%s)" % (f["message"], f["file"]) for f in rep.findings if f["severity"] == "warning"]

    # 2. tools and identity
    if not have("git"):
        blocked.append({"why": "git is not installed", "detail": ["    fix: %s" % install_hint("git")]})
    elif not committed:
        who = (run(["git", "config", "user.name"], cwd=proj).out.strip(), run(["git", "config", "user.email"], cwd=proj).out.strip())
        if not all(who):
            blocked.append({"why": "git does not know your name and email (needed for the first commit)",
                            "detail": ['    fix: git config --global user.name "Your Name"',
                                       '    fix: git config --global user.email "you@yourmail.com"']})
    if a.create:
        if not have("gh"):
            blocked.append({"why": "gh (the GitHub command line) is not installed", "detail": ["    fix: %s" % install_hint("gh")]})
        elif not dry:
            st = run(["gh", "auth", "status"], timeout=45)
            if st.code != 0:
                blocked.append({"why": "gh is not logged in to GitHub", "detail": ["    fix: gh auth login"]})
            elif os.path.isdir(os.path.join(proj, ".github", "workflows")):
                sm = re.search(r"Token scopes?:\s*(.+)", st.out + "\n" + st.err)
                scopes = re.findall(r"'([^']+)'", sm.group(1)) if sm else []
                if scopes and "workflow" not in scopes:     # no scope list at all (fine-grained token) means unknown, not missing
                    blocked.append({"why": "the project has .github/workflows but gh has no permission to push workflows",
                                    "detail": ["    fix: gh auth refresh -h github.com -s workflow"]})
        if os.path.isdir(os.path.join(proj, ".github", "workflows")) and dry:
            notes.append("this project has .github/workflows: gh needs the workflow permission to push it "
                         "(gh auth refresh -h github.com -s workflow)")

    # 3. the plan
    if not is_repo:
        parent = os.path.dirname(proj)
        while parent and parent != os.path.dirname(parent):
            if os.path.exists(os.path.join(parent, ".git")):
                notes.append("%s is inside another git repository (%s); a separate new repository is created here" % (proj, parent))
                break
            parent = os.path.dirname(parent)
        plan.append({"text": "git init -b main", "cmd": ["git", "init", "-b", "main"]})
    if new_lines:
        plan.append({"text": "add %d safe line(s) to .gitignore (%s)" % (len(new_lines), ", ".join(new_lines[:4]) + (", ..." if len(new_lines) > 4 else "")),
                     "gitignore": new_lines})
    if not committed:
        plan.append({"text": "git add -A", "cmd": ["git", "add", "-A"]})
        plan.append({"text": "git commit -m 'Initial site'", "cmd": ["git", "commit", "-m", "Initial site"]})
    else:
        notes.append("this folder already has commits; no new commit is made")
    if a.create:
        if is_repo and has_origin(proj):
            notes.append("a remote called origin already exists; no GitHub repository is created")
        else:
            plan.append({"text": show(["gh", "repo", "create", name, "--" + vis, "--source", ".", "--push"]),
                         "cmd": ["gh", "repo", "create", name, "--" + vis, "--source", ".", "--push"], "create": True})
    if a.create and a.public:
        notes.append("PUBLIC: anyone on the internet can read this code and its history")

    data = {"command": "repo", "dry_run": dry, "name": name, "visibility": vis, "dir": proj, "steps": [p["text"] for p in plan],
            "commands": [p["cmd"] for p in plan if "cmd" in p], "blocked": [b["why"] for b in blocked], "notes": notes,
            "secrets_scan": {"files": rep.stats.get("project_files", 0), "errors": len(errors),
                             "findings": [{"file": f["file"], "line": f["line"], "message": f["message"]} for f in errors[:50]]},
            "url": None}
    lines = []
    if dry:
        lines += ["DRY RUN: nothing has been changed. Add --confirmed to run these steps for real.", ""]
    lines.append('Plan for "%s" (%s) in %s' % (name, vis if a.create else "saved locally only", proj))
    n_files = rep.stats.get("project_files", 0)
    lines.append("  secrets scan of %d file%s: %s" % (n_files, "" if n_files == 1 else "s", "REFUSED" if errors else "clean"))
    for i, p in enumerate(plan, 1):
        lines.append("  %d. %s" % (i, p["text"]))
    if not plan:
        lines.append("  (nothing to do)")
    for n in notes:
        lines.append("  note: %s" % n)
    if blocked:
        lines += ["", "BLOCKED, nothing was changed:"]
        for b in blocked:
            lines.append("  - %s" % b["why"])
            lines += b["detail"]
        emit(data, a.json, lines)
        return 1
    if dry:
        if not a.create:
            lines.append("  (add --create to also make the GitHub repository and push)")
        emit(data, a.json, lines)
        return 0

    # 4. for real
    lines.append("")
    for p in plan:
        if "gitignore" in p:
            write_gitignore(proj, p["gitignore"])
            lines.append("  ok   wrote .gitignore")
            continue
        r = run(p["cmd"], cwd=proj, timeout=300)
        if r.code != 0:
            lines.append("  FAILED  %s" % p["text"])
            lines.append(tail(r.err or r.out))
            hint = repo_hint(r.err + r.out)
            if hint:
                lines.append("  hint: %s" % hint)
            if p.get("create"):
                lines.append("  Your code is committed locally. Fix the problem above and run again.")
            emit(data, a.json, lines)
            return 1
        lines.append("  ok   %s" % p["text"])
        if p.get("create"):
            m = re.search(r"https://github\.com/\S+", r.out + "\n" + r.err)
            data["url"] = m.group(0) if m else None
    if data["url"]:
        lines.append("Repository: %s" % data["url"])
    elif not a.create:
        lines.append("Saved locally. Run again with --create --confirmed to put it on GitHub.")
    emit(data, a.json, lines)
    return 0


# ---- deploy ----

def safe_arg(path):
    return "./" + path if path.startswith("-") else path


def check_name(value, what):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value or ""):
        raise UsageError("'%s' is not a valid %s" % (value, what))
    return value


def deploy_hint(text):
    low = text.lower()
    if "team_access_required" in low or "must have access to the team" in low:
        return "Vercel does not accept the git author of the last commit. Set git config user.email to an email that belongs to the " \
               "Vercel team, or deploy from the folder with this command instead of from git."
    if "no existing credentials" in low or "not logged in" in low or "please log in" in low:
        return "Log in first: vercel login"
    if "scope" in low and ("specify" in low or "required" in low):
        return "Vercel needs to know which account to use: add --scope YOUR-TEAM-SLUG"
    return ""


GENERIC_NAMES = frozenset(("dist", "build", "out", "public", "site", "www", "docs", "_site", "output", "html", "web", "website", "app", "src",
                           "my-site", "mysite", "test", "tests", "tmp", "temp", "project", "projects", "untitled", "new-project", "home",
                           "downloads", "desktop", "documents", "users", "var", "private", "folders"))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")


def derive_project(abs_dir):
    """Vercel project name from the folder: its own name, or the parent's when it is called dist, build and so on.
    None when the result would be too generic to be safe."""
    path = abs_dir.rstrip(os.sep)
    base = os.path.basename(path)
    name = slug(base)
    if len(name) >= 3 and name not in GENERIC_NAMES:
        return name
    parent = slug(os.path.basename(os.path.dirname(path)))
    return parent if len(parent) >= 3 and parent not in GENERIC_NAMES else None


def cmd_deploy(a):
    d = a.dir
    if d.startswith("-"):
        raise UsageError("--dir must not start with '-'")
    if not os.path.isdir(d):
        raise UsageError("no such folder: %s (build the site first)" % d)
    scope = check_name(a.scope, "scope") if a.scope else None
    project = check_name(a.project, "project name") if a.project else None
    dry = not a.confirmed
    abs_dir = os.path.abspath(d)
    linked = os.path.isfile(os.path.join(abs_dir, ".vercel", "project.json"))
    source = "the --project option" if project else ("the folder's existing link" if linked else None)
    if not project and not linked:
        project = derive_project(abs_dir)
        source = "the folder name; use --project NAME to choose another" if project else None
    cmds = []
    if project:
        cmds.append({"cmd": ["vercel", "link", "--yes", "--project", project] + (["--scope", scope] if scope else []), "cwd": abs_dir})
    cmds.append({"cmd": ["vercel", "deploy", safe_arg(d), "--yes"] + (["--prod"] if a.prod else []) + (["--scope", scope] if scope else []),
                 "cwd": None})

    rep = check_site.run_checks(abs_dir)
    errors = [f for f in rep.findings if f["severity"] == "error"]
    secret_errors = [f for f in errors if f["rule"].startswith("secrets.")]
    verdict = "PASS" if not errors else ("REFUSED" if (secret_errors or not a.force) else "FORCED")
    blocked = []
    if secret_errors:
        blocked.append("secrets in the site folder would be published. --force does not override this: remove them and rotate the keys")
    elif errors and not a.force:
        blocked.append("fix the site check errors above (python3 scripts/check_site.py %s shows everything), or add --force to publish anyway" % d)
    if not project and not linked:
        blocked.append("this folder is not linked to a Vercel project and its name (%s) is too generic: Vercel would reuse any project "
                       "called that, which can overwrite another site. Add --project NAME, a short name unique to this site"
                       % (os.path.basename(abs_dir.rstrip(os.sep)) or abs_dir))
    if not have("vercel"):
        blocked.append("vercel is not installed. Fix: %s" % install_hint("vercel"))
    data = {"command": "deploy", "dry_run": dry, "production": bool(a.prod), "dir": d, "commands": [c["cmd"] for c in cmds],
            "project": project or ("linked" if linked else None), "project_from": source, "blocked": list(blocked), "url": None,
            "check": {"errors": rep.errors, "warnings": rep.warnings, "verdict": verdict,
                      "findings": [{"rule": f["rule"], "file": f["file"], "line": f["line"], "message": f["message"]} for f in errors[:50]]}}
    lines = []
    if dry:
        lines += ["DRY RUN: nothing has been published. Add --confirmed to run this for real.", ""]
    lines.append("Site check of %s: %s (%d errors, %d warnings)" % (d, verdict, rep.errors, rep.warnings))
    if errors:
        lines += finding_lines(errors[:12])
        if len(errors) > 12:
            lines.append("    ...and %d more" % (len(errors) - 12))
    lines.append("  target: %s" % ("PRODUCTION (the public address)" if a.prod else "preview"))
    if project or linked:
        lines.append("  vercel project: %s (from %s)" % (project or "the linked one", source))
    for c in cmds:
        lines.append(("  would run: %s" if dry else "  running: %s") % show(c["cmd"]))
    if errors and a.force and not secret_errors:
        lines.append("  FORCED: publishing although the check found %d error(s)" % len(errors))
    if blocked:
        lines += ["", "BLOCKED, nothing was published:"] + ["  - %s" % b for b in blocked]
        emit(data, a.json, lines)
        return 1
    if dry:
        emit(data, a.json, lines)
        return 0

    who = run(["vercel", "whoami"], timeout=60)
    if who.code != 0:
        data["blocked"].append("vercel is not logged in")
        lines += ["", "BLOCKED: vercel is not logged in. Fix: vercel login"]
        emit(data, a.json, lines)
        return 1
    result = None
    for c in cmds:
        result = run(c["cmd"], cwd=c["cwd"], timeout=1200)
        if result.code != 0:
            lines += ["", "FAILED: %s" % show(c["cmd"]), tail(result.err or result.out, 15)]
            hint = deploy_hint(result.err + result.out)
            if hint:
                lines.append("  hint: %s" % hint)
            emit(data, a.json, lines)
            return 1
    text = result.out + "\n" + result.err
    alias = re.search(r"Aliased:\s*(https://\S+)", text)
    urls = [u for u in re.findall(r"https://[A-Za-z0-9.\-]+\.vercel\.app\S*", text) if "vercel.com" not in u]
    data["url"] = alias.group(1) if alias else (urls[0] if urls else None)
    lines.append("")
    if data["url"]:
        lines.append("Published (%s): %s" % ("production" if a.prod else "preview", data["url"]))
        lines.append("Next: open the address and confirm the page really loads.")
    else:
        lines.append("Vercel finished without printing an address. Open your Vercel dashboard to find it.")
    emit(data, a.json, lines)
    return 0


# ---- domain-help ----

def parse_domain(raw):
    d = (raw or "").strip().lower()
    d = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", d)
    d = d.split("/")[0].split("?")[0].split("#")[0].rstrip(".")
    d = d.split("@")[-1].split(":")[0]
    try:
        d = d.encode("idna").decode("ascii")
    except UnicodeError:
        raise UsageError("'%s' is not a valid domain name" % raw)
    labels = d.split(".")
    if (len(d) > 253 or len(labels) < 2 or not all(re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", l) for l in labels)
            or re.fullmatch(r"[\d.]+", d) or labels[-1].isdigit()):
        raise UsageError("'%s' is not a valid domain name (example: mysite.com)" % raw)
    if d.endswith(".vercel.app") or d == "localhost":
        raise UsageError("%s needs no DNS records; it already works" % d)
    return d


def cmd_domain_help(a):
    d = parse_domain(a.domain)
    labels = d.split(".")
    base = 2 if ".".join(labels[-2:]) not in SECOND_LEVEL else 3
    if len(labels) > base and labels[0] == "www" and len(labels) == base + 1:
        d = ".".join(labels[1:])
        labels = d.split(".")
    is_apex = len(labels) <= base
    records = []
    if is_apex:
        records.append({"type": "A", "name": "@", "host": d, "value": DNS_A})
        records.append({"type": "CNAME", "name": "www", "host": "www." + d, "value": DNS_CNAME})
    else:
        sub = ".".join(labels[:-base])
        records.append({"type": "CNAME", "name": sub, "host": d, "value": DNS_CNAME})
    data = {"command": "domain-help", "domain": d, "apex": is_apex, "records": records, "changes_made": False,
            "nameserver_alternative": ["ns1.vercel-dns.com", "ns2.vercel-dns.com"]}
    lines = ["DNS setup for %s (this command changes nothing; you add these at your domain registrar)" % d, "",
             "1. Tell Vercel about the domain: open your project on vercel.com, Settings, Domains, Add '%s'." % d,
             "   (command-line alternative: vercel domains add %s)" % d, "",
             "2. At the registrar's DNS page (where you bought the domain), add:", "",
             "     Type    Name/Host    Value"]
    for r in records:
        lines.append("     %-7s %-12s %s" % (r["type"], r["name"], r["value"]))
    lines += ["",
              "   Remove any older A, AAAA or CNAME record for the same name (parking pages) first, or the new one is ignored.",
              "   '@' means the bare domain; some registrars want that field left blank.",
              "3. Wait. It usually works within minutes and can take up to 48 hours. Vercel adds the HTTPS certificate by itself.",
              "4. Check: Vercel's Domains page shows a green check. If it shows different values than the ones above, use Vercel's.",
              "",
              "Alternative: point the domain's nameservers to ns1.vercel-dns.com and ns2.vercel-dns.com (Vercel then manages all DNS)."]
    if is_apex:
        lines.append("In Vercel's Domains settings, choose whether www redirects to %s or the other way round." % d)
    emit(data, a.json, lines)
    return 0


# ---- CLI ----

class OneLineParser(argparse.ArgumentParser):
    """argparse, but a usage mistake is one line on stderr (exit 2) instead of a wall of usage text."""

    def error(self, message):
        sys.stderr.write("error: %s (see %s --help)\n" % (message, self.prog))
        raise SystemExit(2)


def build_parser():
    ap = OneLineParser(prog="ship.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("doctor", help="check that git, gh and vercel are installed and logged in",
                       description="Read-only. Reports what is installed and logged in, with the one command that fixes each gap.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("repo", help="commit the project and (with --create) make the GitHub repository; dry run unless --confirmed",
                       description="Writes a safe .gitignore, makes the first commit 'Initial site' and, with --create, creates the GitHub "
                                   "repository and pushes. Without --confirmed it only prints the plan. It refuses, before changing anything, "
                                   "if the files a commit would contain hold secrets.")
    p.add_argument("name", help="repository name, for example my-site (OWNER/NAME for an organisation)")
    p.add_argument("--dir", default=".", help="project folder (default: current folder)")
    vis = p.add_mutually_exclusive_group()
    vis.add_argument("--private", action="store_true", help="private repository (the default)")
    vis.add_argument("--public", action="store_true", help="public repository: anyone can read the code")
    p.add_argument("--create", action="store_true", help="also create the repository on GitHub and push")
    p.add_argument("--confirmed", action="store_true", help="really do it (only after the owner said yes)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_repo)

    p = sub.add_parser("deploy", help="check the site, then publish it with vercel; dry run unless --confirmed",
                       description="Runs check_site.py on the folder, then publishes it with vercel (a preview, or production with --prod). "
                                   "Without --confirmed it only prints the check result, the project name and the exact commands.")
    p.add_argument("--dir", default="dist", help="built site folder (default: dist)")
    p.add_argument("--prod", action="store_true", help="publish to production (the public address) instead of a preview")
    p.add_argument("--confirmed", action="store_true", help="really do it (only after the owner said yes)")
    p.add_argument("--force", action="store_true", help="publish although check_site.py found errors (never overrides secrets)")
    p.add_argument("--scope", help="Vercel team or account slug, when the CLI asks which one to use")
    p.add_argument("--project", help="name of the Vercel project (default: taken from the site's folder name)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("domain-help", help="print the DNS records to add for a domain; changes nothing",
                       description="Prints the DNS records to add at the registrar for an apex domain, a www name or a subdomain. Makes no changes.")
    p.add_argument("domain", help="for example mysite.com or www.mysite.com or shop.mysite.com")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_domain_help)
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
    except (ValueError, OSError):
        pass
    try:
        return a.func(a)
    except UsageError as ex:
        sys.stderr.write("error: %s\n" % ex)
        return 2
    except OSError as ex:
        sys.stderr.write("error: %s\n" % ex)
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return 130


def run_cli():
    try:
        code = main()
        sys.stdout.flush()
        return code
    except BrokenPipeError:          # for example `ship.py doctor | head`
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 0


if __name__ == "__main__":
    sys.exit(run_cli())
