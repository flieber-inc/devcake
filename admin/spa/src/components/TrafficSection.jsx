import React, { useState } from "react";
import { Play } from "lucide-react";
import { send } from "../api.js";
import { Section } from "./Card.jsx";
import { Help, Input, Select } from "./Field.jsx";
import SettingRow from "./SettingRow.jsx";
import Button from "./Button.jsx";
import Toggle from "./Toggle.jsx";
import ImmediateBadge from "./ImmediateBadge.jsx";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";

export default function TrafficSection() {
  const { dr, healthInfo } = useSharedDraft();
  const [mapperMsg, setMapperMsg] = useState("");

  const cfg = dr.draft.cfg;
  const setField = dr.setField;

  const runMapper = async () => {
    setMapperMsg("Starting…");
    try {
      const r = await send("POST", "/relations-mapper/run");
      setMapperMsg(`✓ dispatched ${r.run_id} — watch it on the Runs page`);
    } catch (e) {
      setMapperMsg(`✗ ${String(e.message || e)}`);
    }
  };

  const rm = cfg.relations_mapper || { enabled: false, interval_minutes: 60, dev_type: null };
  const serverRm = dr.server.cfg.relations_mapper || {};
  const mapperDirty = dr.diff.some((x) => x.path.startsWith("cfg.relations_mapper"));

  return (
      <Section id="traffic" title="Traffic control"
        description="Mission breakdown depth and the Relations Mapper. (Mission intake is the master switch in the sidebar — it applies immediately.)">
        <div className="divide-y divide-neutral-100 dark:divide-neutral-800">
          <SettingRow label="Decomposition depth"
            desc="How many generations of Mission breakdown ONBOARD may create."
            help="Each ONBOARD pass may split a high-complexity Mission into sub-missions. At 1, a Mission created by a breakdown is never broken down again. At 2 (default), it may be broken down once more — a Project's missions can each split again. Unlimited removes the ceiling entirely and leaves the choice to the ONBOARD Dev on every pass: a runaway Dev could keep splitting work indefinitely.">
            <Select className="w-40" value={String(cfg.max_decomposition_depth)}
              aria-label="Decomposition depth limit"
              onChange={(e) => setField("cfg.max_decomposition_depth", Number(e.target.value))}>
              {![0, 1, 2].includes(cfg.max_decomposition_depth) && (
                // an API/YAML-set depth outside the offered values must
                // round-trip — a controlled select with no matching option
                // would misrender it and the next save would clobber it
                <option value={String(cfg.max_decomposition_depth)}>
                  {cfg.max_decomposition_depth} levels (set via API)
                </option>
              )}
              <option value="1">1 level</option>
              <option value="2">2 levels</option>
              <option value="0">Unlimited</option>
            </Select>
          </SettingRow>
        </div>
        <div className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm font-semibold">
              Relations Mapper
              <Help text="A Dev tasked strictly with mapping missing 'blocked by' relations between existing missions (it reads every open mission's title and description head). Proposed relations are validated by the app and appear in your PMO; delete a relation there to undo it." />
            </span>
            <div className="flex items-center gap-2">
              <ImmediateBadge text="runs with saved settings" />
              <Button kind="ghost" icon={Play} onClick={runMapper}
                disabled={!serverRm.dev_type || mapperDirty}
                title={mapperDirty
                  ? "Save your mapper changes first — Run now uses the saved settings"
                  : !serverRm.dev_type ? "Pick and save a Dev Type first" : undefined}>
                Run now
              </Button>
            </div>
          </div>
          <div className="divide-y divide-neutral-100 dark:divide-neutral-800">
            <SettingRow label="Dev Type"
              desc="Which Dev Type runs the mapper."
              help="The seeded mapper (a cheap, fast model) is the default — ordering judgment from titles and description heads doesn't need a heavyweight.">
              <Select className="w-44" value={rm.dev_type || ""}
                aria-label="Relations Mapper Dev Type"
                onChange={(e) => {
                  setField("cfg.relations_mapper.dev_type", e.target.value || null);
                  if (!e.target.value) setField("cfg.relations_mapper.enabled", false);
                }}>
                <option value="">(none)</option>
                {dr.order.map((n) => <option key={n} value={n}>{n}</option>)}
              </Select>
            </SettingRow>
            <SettingRow label="Interval"
              desc="Minutes between automatic passes."
              help="Cadence of the periodic service when enabled. The first automatic run happens one interval after the app starts; use Run now for an immediate pass.">
              <Input type="number" className="w-24" min="1" value={rm.interval_minutes}
                aria-label="Relations Mapper interval (minutes)"
                onChange={(e) => setField("cfg.relations_mapper.interval_minutes", Number(e.target.value))}
                onBlur={(e) => setField("cfg.relations_mapper.interval_minutes",
                  Math.max(1, Number(e.target.value) || 60))} />
            </SettingRow>
            <SettingRow label="Periodic service"
              desc={rm.enabled ? "ON — runs on the interval." : "OFF — manual only (default)."}>
              <Toggle on={rm.enabled} label="Periodic service"
                onClick={() => {
                  if (!rm.enabled && !rm.dev_type) {
                    setMapperMsg("✗ pick a Dev Type first");
                    return;
                  }
                  setField("cfg.relations_mapper.enabled", !rm.enabled);
                }} />
            </SettingRow>
          </div>
          {healthInfo?.mapper_degraded && (
            <p className="text-sm text-amber-600 dark:text-amber-400">
              ⚠ Periodic service backing off — the last 3 mapper runs failed
              ({healthInfo.mapper_degraded}). Run now still works; a successful run resumes
              the schedule.
            </p>
          )}
          {mapperMsg && (
            <p className={`text-sm ${mapperMsg.startsWith("✗") ? "text-red-600" : "text-green-700 dark:text-green-400"}`}>
              {mapperMsg}
            </p>
          )}
        </div>
      </Section>
  );
}
