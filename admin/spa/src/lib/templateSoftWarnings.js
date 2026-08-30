// Soft (non-blocking) playbook health checks for the Prompts template
// manager Save path. Mirrors templates.template_warnings wording so
// operators see the same dialect at save time and on Overview/health.
// Hard failures (unknown placeholders, empty, size) stay on the API
// validate_template path.

/**
 * @param {{
 *   missionType: string,
 *   templateName: string,
 *   text: string,
 *   maxDecompositionDepth?: number | null,
 * }} opts
 * @returns {string[]}
 */
export function templateSoftWarnings({
  missionType,
  templateName,
  text,
  maxDecompositionDepth,
}) {
  const body = text || "";
  const name = templateName || "";
  const mt = missionType || "";
  const warns = [];

  if (mt === "ONBOARD" && body.includes("executed_trivially")) {
    warns.push(
      `ONBOARD template '${name}' still instructs the removed `
      + "executed_trivially outcome — its runs will park with "
      + "DEVCAKE-SKIP; re-save it from the current default",
    );
  }

  if (body.includes("Work ONLY inside")) {
    warns.push(
      `${mt} template '${name}' still carries the unqualified `
      + '"Work ONLY inside /workspace/repo/…" rule, which '
      + "contradicts the /workspace/out/result.json rule — Devs "
      + "may write result.json into the repository and fail the "
      + "run. Re-save it from the current default.",
    );
  }

  if (
    mt === "ONBOARD"
    && maxDecompositionDepth != null
    && maxDecompositionDepth !== 1
    && !body.includes("{decomposition_rule}")
  ) {
    const shown = maxDecompositionDepth || "unlimited";
    warns.push(
      `ONBOARD: active prompt template '${name}' has no `
      + "{decomposition_rule} placeholder — the configured "
      + `decomposition depth (${shown}) cannot reach the Dev `
      + "prompt; re-add the placeholder or switch to a built-in "
      + "template",
    );
  }

  return warns;
}
