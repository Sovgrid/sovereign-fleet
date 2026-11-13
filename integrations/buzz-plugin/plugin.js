export const inject = ["agents", "host"];

function parseJson(raw, label) {
  if (typeof raw !== "string" || !raw.trim()) {
    throw new Error(`fleet ${label} returned no output (is fleet on PATH and configured?)`);
  }
  try {
    return JSON.parse(raw);
  } catch (err) {
    throw new Error(`fleet ${label} returned invalid JSON: ${err.message}`);
  }
}

function formatWhen(epochSeconds) {
  if (typeof epochSeconds !== "number" || !Number.isFinite(epochSeconds) || epochSeconds <= 0) {
    return "?";
  }
  return new Date(epochSeconds * 1000).toISOString().replace("T", " ").slice(0, 16) + "Z";
}

function formatTaskDetail(t) {
  const r = t.result || {};
  const landing = r.landing || {};
  const lines = [
    `# ${t.id} [${t.status}]`,
    `Task: ${t.text || ""}`,
    `Created: ${formatWhen(t.created_at)}${t.source ? ` | source: ${t.source}` : ""}`,
  ];
  if (t.issue && t.issue.number) {
    lines.push(`Issue: ${t.issue.repo || ""}#${t.issue.number} ${t.issue.url || ""}`.trim());
  }
  const tried = Array.isArray(r.tried_lanes) ? r.tried_lanes.filter(Boolean) : [];
  lines.push(`Lane: ${t.lane || "auto"}${tried.length > 1 ? ` (tried: ${tried.join(", ")})` : ""}`);
  if (r.exit_code !== undefined && r.exit_code !== null) {
    lines.push(`Exit code: ${r.exit_code}`);
  }
  const prUrl = landing.pr_url || r.pr_url;
  const commit = landing.commit || r.commit || r.commit_hash;
  if (prUrl) lines.push(`Pull request: ${prUrl}`);
  if (commit) lines.push(`Commit: ${commit}${landing.status ? ` (${landing.status})` : ""}`);
  if (r.branch) lines.push(`Branch: ${r.branch}`);
  const failed = t.status === "failed" || t.status === "cancelled" || (r.exit_code !== undefined && r.exit_code !== null && r.exit_code !== 0);
  const autopsy = t.autopsy || r.autopsy || {};
  const cause = r.error || (typeof autopsy.root_cause === "string" ? autopsy.root_cause.replace(/^[\s|]+|[\s|]+$/g, "") : "");
  if (failed && cause) {
    lines.push(`Why it failed: ${cause}`);
    if (autopsy.blocker_file && autopsy.blocker_file !== "unknown") {
      lines.push(`Blocker: ${autopsy.blocker_file}`);
    }
    if (autopsy.suggested_action) {
      lines.push(`Suggestion: ${autopsy.suggested_action}`);
    }
  }
  if (Array.isArray(r.changed_files) && r.changed_files.length > 0) {
    lines.push(`Changed files (${r.changed_files.length}): ${r.changed_files.slice(0, 30).join(", ")}`);
  }
  if (Array.isArray(r.log_tail) && r.log_tail.length > 0) {
    lines.push("Log (last lines):\n" + r.log_tail.slice(-8).map((l) => `  ${l}`).join("\n"));
  }
  if (t.status === "awaiting_approval") {
    lines.push("Waiting for human approval in the Sovereign Fleet dashboard or Matrix.");
  }
  return lines.join("\n");
}

export function apply(ctx) {
  ctx.agents.addTool({
    name: "fleet_submit",
    description: "Queue a coding task in Sovereign Fleet. Always lands as awaiting_approval for the owner to approve; never runs unattended.",
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string", description: "Clear task instruction for the fleet." },
        project: { type: "string", description: "Target project key from fleet.toml (recommended)." },
        allowed_paths: {
          type: "array",
          items: { type: "string" },
          description: "Optional glob patterns the change must stay within.",
        },
        gate: {
          type: "string",
          description: "Optional test gate, e.g. 'pytest:tests/test_cli.py' or 'npm:test'.",
        },
        lane: { type: "string", description: "Optional lane id; omit to let the fleet router pick." },
      },
      required: ["text"],
    },
    async call(_client, _context, args) {
      const text = typeof args?.text === "string" ? args.text.trim() : "";
      if (!text) throw new Error("text is required");
      const payload = { text, source: "buzz_plugin" };
      if (typeof args?.project === "string" && args.project.trim()) payload.project = args.project.trim();
      if (Array.isArray(args?.allowed_paths) && args.allowed_paths.length > 0) {
        payload.allowed_paths = args.allowed_paths;
      }
      if (args?.gate !== undefined && args?.gate !== null && args.gate !== "") payload.gate = args.gate;
      if (typeof args?.lane === "string" && args.lane.trim()) payload.lane = args.lane.trim();

      const out = await ctx.host.runCommand("submit", JSON.stringify(payload));
      const task = parseJson(out, "task submit");
      return (
        `Queued ${task.id} (${task.status}; lane ${task.lane || "picked on approval"}).\n` +
        `Waiting for human approval in the Sovereign Fleet dashboard or Matrix.\n` +
        `Follow it with fleet_task(task_id="${task.id}").`
      );
    },
  });

  ctx.agents.addTool({
    name: "fleet_task",
    description: "Read status, lane, gate results, changed files and landing details of one Sovereign Fleet task.",
    inputSchema: {
      type: "object",
      properties: {
        task_id: { type: "string", description: "The Sovereign Fleet task id." },
      },
      required: ["task_id"],
    },
    async call(_client, _context, args) {
      const taskId = typeof args?.task_id === "string" ? args.task_id.trim() : "";
      if (!taskId) throw new Error("task_id is required");
      const out = await ctx.host.runCommand("show", taskId);
      const task = parseJson(out, "task show");
      return formatTaskDetail(task);
    },
  });

  ctx.agents.addTool({
    name: "fleet_tasks",
    description: "Show Sovereign Fleet task counts (open, awaiting approval, completed, failed) and recent tasks.",
    inputSchema: {
      type: "object",
      properties: {
        status: {
          type: "string",
          enum: ["open", "awaiting_approval", "queued", "in_progress", "completed", "failed", "all"],
          description: "Filter listed tasks (default: open).",
        },
        limit: {
          type: "integer",
          minimum: 1,
          maximum: 50,
          description: "Maximum tasks to list (default: 15).",
        },
      },
    },
    async call(_client, _context, args) {
      const out = await ctx.host.runCommand("list");
      const all = parseJson(out, "task list");
      const openStatuses = new Set(["queued", "in_progress", "awaiting_approval"]);
      const counts = {
        total: all.length,
        open: 0,
        awaiting_approval: 0,
        completed: 0,
        failed: 0,
      };
      for (const t of all) {
        if (openStatuses.has(t.status)) counts.open += 1;
        if (t.status === "awaiting_approval") counts.awaiting_approval += 1;
        if (t.status === "completed") counts.completed += 1;
        if (t.status === "failed") counts.failed += 1;
      }
      const filter = typeof args?.status === "string" && args.status ? args.status : "open";
      const limit = Math.max(1, Math.min(Number(args?.limit) || 15, 50));
      const filtered = all.filter((t) => {
        if (filter === "all") return true;
        if (filter === "open") return openStatuses.has(t.status);
        return t.status === filter;
      });
      const lines = [
        `Counts: ${counts.open} open (${counts.awaiting_approval} awaiting approval), ${counts.completed} completed, ${counts.failed} failed (${counts.total} total).`,
      ];
      if (filtered.length === 0) {
        lines.push(`No tasks matching '${filter}'.`);
        return lines.join("\n");
      }
      lines.push(`Showing ${Math.min(filtered.length, limit)} of ${filtered.length} (${filter}), newest first:`);
      for (const t of filtered.slice(0, limit)) {
        const title = String(t.text || "").split("\n")[0].slice(0, 80);
        const proj = t.project ? ` (${t.project})` : "";
        lines.push(`- ${t.id} [${t.status}] ${t.lane || "auto"}${proj} ${formatWhen(t.created_at)}: ${title}`);
      }
      return lines.join("\n");
    },
  });

  ctx.agents.addTool({
    name: "fleet_lanes",
    description: "Inspect Sovereign Fleet lane health from the last stored check.",
    inputSchema: { type: "object", properties: {} },
    async call() {
      const out = await ctx.host.runCommand("lanes");
      const data = parseJson(out, "lanes show");
      const lanes = data.lanes || {};
      const names = Object.keys(lanes);
      if (names.length === 0) return "No lanes recorded.";
      const lines = ["Fleet lanes:"];
      for (const name of names) {
        const rec = lanes[name] || {};
        const reason = rec.reason ? ` (${rec.reason})` : "";
        lines.push(`- ${name}: ${String(rec.status || "unknown").toUpperCase()}${reason}`);
      }
      return lines.join("\n");
    },
  });
}
