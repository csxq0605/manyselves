const runIdSegment = /^[a-z][a-z0-9-]*-[0-9a-f]{32}$/;

/**
 * A short name for an Agent id.
 *
 * Workflow Agents carry ids such as
 * `distribution-reporting:full-report-<run>:report-consultant:run:full-report-<run>:report-consultant:writer:2.4#1`.
 * The Run id, the Capability and repeated role names say nothing a person
 * needs; what is left (`report-consultant · writer · 2.4#1`) names the role and
 * the piece of work.
 */
export function shortAgentLabel(agentId: string): string {
  const parts = agentId.split(":").filter((part) => part && part !== "run" && !runIdSegment.test(part));
  if (parts.length <= 1) return parts[0] ?? agentId;
  const unique: string[] = [];
  for (const part of parts) {
    if (!unique.includes(part)) unique.push(part);
  }
  // The leading Capability id is the same for every Agent of a Run.
  const named = unique.length > 2 && agentId.includes(":run:") ? unique.slice(1) : unique;
  return named.join(" · ");
}
