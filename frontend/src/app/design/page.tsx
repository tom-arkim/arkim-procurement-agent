/**
 * /design — Component showcase page
 * Renders every Phase 2 component in its key states so visual QA can be done
 * without standing up a real sourcing run.
 */

import { Button } from "@/components/ui/button";
import { Pill, Dot } from "@/components/ui/pill";
import { PnMatch, CompatBadge, MatchScore, MatchBar } from "@/components/ui/match";
import { PhaseBar } from "@/components/ui/phase";
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from "@/components/ui/card";
import { ConfidenceIndicator } from "@/components/ui/confidence-indicator";
import { TierHeader } from "@/components/gofer/tier-header";
import { RunSummaryBar } from "@/components/gofer/run-summary-bar";
import { AssetPanel } from "@/components/gofer/asset-panel";
import {
  ArrowRight, Check, X, ChevronDown, ChevronRight, Search, Filter,
  Send, Copy, History, Edit, External, Warn, Stack, Pin, Plus,
  Package, Building, Settings, User, Clock, Truck, Network, Globe, Dollar,
} from "@/components/ui/icons";
import type { Phase, AssetSpecs } from "@/types";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const PHASES: Phase[] = [
  "intake", "sourcing", "comparison", "pending_first_approval", "completed",
];

const MOCK_SPECS: AssetSpecs = {
  manufacturer: "Grundfos",
  model: "CM5-4 A-R-I-E-AVBE",
  part_number: "96806877",
  detected_type: "Centrifugal Pump",
  category: "Equipment",
  manufacturer_confidence: 94,
  part_id_confidence: 81,
  hp: "0.75",
  rpm: "3450",
  voltage: "230/460V",
  frame: "56J",
  gpm: "12",
  psi: "58",
  impeller_size: "3.5\"",
  mech_seal: "Carbon/Ceramic",
  material_spec: "316 SS",
  urgency_factor: 0.7,
  warranty_status: "Expired",
};

const MOCK_RUN = {
  id: "run_abc12345",
  phase: "comparison" as Phase,
  urgency: "Emergency" as const,
  warranty: "Expired" as const,
  facility_id: "fac_houston_01",
  asset_summary: "Grundfos CM5-4 · Centrifugal Pump",
  amount: 1248.00,
};

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function DesignPage() {
  return (
    <div
      className="min-h-screen p-8"
      style={{ background: "var(--bg-1)" }}
    >
      <div className="mx-auto max-w-[960px] flex flex-col gap-10">

        {/* Page title */}
        <div className="flex flex-col gap-1">
          <h1 className="text-h1 text-fg-1">Design System</h1>
          <p className="text-sm text-fg-3">Phase 2 component showcase — all key states</p>
        </div>

        {/* ----------------------------------------------------------------- */}
        <Section title="Icons">
          <div className="flex flex-wrap gap-4 text-fg-2">
            {[
              ["ArrowRight", <ArrowRight key="ar" />],
              ["Check", <Check key="ch" />],
              ["X", <X key="x" />],
              ["ChevronDown", <ChevronDown key="cd" />],
              ["ChevronRight", <ChevronRight key="cr" />],
              ["Search", <Search key="se" />],
              ["Filter", <Filter key="fi" />],
              ["Send", <Send key="sn" />],
              ["Copy", <Copy key="co" />],
              ["History", <History key="hi" />],
              ["Edit", <Edit key="ed" />],
              ["External", <External key="ex" />],
              ["Warn", <Warn key="wa" />],
              ["Stack", <Stack key="st" />],
              ["Pin", <Pin key="pi" />],
              ["Plus", <Plus key="pl" />],
              ["Package", <Package key="pa" />],
              ["Building", <Building key="bu" />],
              ["Settings", <Settings key="sg" />],
              ["User", <User key="us" />],
              ["Clock", <Clock key="cl" />],
              ["Truck", <Truck key="tr" />],
              ["Network", <Network key="ne" />],
              ["Globe", <Globe key="gl" />],
              ["Dollar", <Dollar key="do" />],
            ].map(([name, el]) => (
              <div key={String(name)} className="flex flex-col items-center gap-1.5">
                <div className="flex h-8 w-8 items-center justify-center rounded bg-bg-3 border border-hr-2">
                  {el}
                </div>
                <span className="font-mono text-[9px] text-fg-4 uppercase tracking-[0.06em]">
                  {String(name)}
                </span>
              </div>
            ))}
          </div>
        </Section>

        {/* ----------------------------------------------------------------- */}
        <Section title="Buttons">
          <div className="flex flex-col gap-4">
            {/* Variants row */}
            <Row label="Variants">
              <Button variant="primary">Primary</Button>
              <Button variant="secondary">Secondary</Button>
              <Button variant="ghost">Ghost</Button>
              <Button variant="destructive">Destructive</Button>
              <Button variant="success">Success</Button>
              <Button variant="warning">Warning</Button>
              <Button variant="outreach">Outreach</Button>
            </Row>

            {/* Sizes */}
            <Row label="Sizes">
              <Button size="sm">Small</Button>
              <Button size="md">Medium</Button>
              <Button size="lg">Large</Button>
            </Row>

            {/* Icons */}
            <Row label="With icons">
              <Button variant="primary" leadingIcon={<Search size={12} />}>Search</Button>
              <Button variant="secondary" trailingIcon={<ArrowRight size={12} />}>Next</Button>
              <Button variant="outreach" leadingIcon={<Send size={12} />}>Send outreach</Button>
            </Row>

            {/* States */}
            <Row label="States">
              <Button variant="primary" loading>Loading</Button>
              <Button variant="secondary" disabled>Disabled</Button>
            </Row>
          </div>
        </Section>

        {/* ----------------------------------------------------------------- */}
        <Section title="Pills & Dots">
          <div className="flex flex-col gap-3">
            <Row label="Tones">
              <Pill tone="blue">Blue</Pill>
              <Pill tone="green">Green</Pill>
              <Pill tone="amber">Amber</Pill>
              <Pill tone="red">Red</Pill>
              <Pill tone="ghost">Ghost</Pill>
            </Row>
            <Row label="Solid">
              <Pill tone="blue" solid>Blue</Pill>
              <Pill tone="green" solid>Green</Pill>
              <Pill tone="amber" solid>Amber</Pill>
              <Pill tone="red" solid>Red</Pill>
              <Pill tone="ghost" solid>Ghost</Pill>
            </Row>
            <Row label="With dot">
              <Pill tone="blue" dot>Sourcing</Pill>
              <Pill tone="green" pulseDot>Live</Pill>
              <Pill tone="amber" dot>Pending</Pill>
              <Pill tone="red" pulseDot>Emergency</Pill>
            </Row>
            <Row label="Dot component">
              {(["blue", "green", "amber", "red", "ghost"] as const).map((t) => (
                <div key={t} className="flex items-center gap-1.5">
                  <Dot tone={t} />
                  <Dot tone={t} pulse />
                  <span className="font-mono text-[10px] text-fg-4">{t}</span>
                </div>
              ))}
            </Row>
          </div>
        </Section>

        {/* ----------------------------------------------------------------- */}
        <Section title="Match Indicators">
          <div className="flex flex-col gap-3">
            <Row label="P/N match">
              <PnMatch level="exact" />
              <PnMatch level="normalized" />
              <PnMatch level="stem" />
              <PnMatch level="substring" />
              <PnMatch level="none" />
            </Row>
            <Row label="Compat badge">
              <CompatBadge summary="fit_confirmed" />
              <CompatBadge summary="fit_likely" />
              <CompatBadge summary="verification_required" />
              <CompatBadge summary="incompatible" />
            </Row>
            <Row label="Match score">
              <MatchScore score={96} />
              <MatchScore score={82} />
              <MatchScore score={68} />
              <MatchScore score={44} />
            </Row>
            <div className="flex flex-col gap-2 max-w-[280px]">
              <MatchBar value={96} label="96%" />
              <MatchBar value={82} label="82%" />
              <MatchBar value={68} label="68%" />
              <MatchBar value={44} label="44%" />
            </div>
          </div>
        </Section>

        {/* ----------------------------------------------------------------- */}
        <Section title="Phase Bar">
          <div className="flex flex-col gap-4">
            {PHASES.map((ph) => (
              <div key={ph} className="flex flex-col gap-1">
                <span className="font-mono text-[10px] text-fg-4 uppercase tracking-[0.08em]">{ph}</span>
                <PhaseBar phase={ph} />
              </div>
            ))}
          </div>
        </Section>

        {/* ----------------------------------------------------------------- */}
        <Section title="Cards">
          <div className="grid grid-cols-2 gap-3">
            <Card variant="default">
              <CardHeader>
                <CardTitle>Default card</CardTitle>
                <CardDescription>bg-3 surface · card shadow</CardDescription>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-fg-2">Card body content goes here.</p>
              </CardContent>
              <CardFooter>
                <Button size="sm">Action</Button>
              </CardFooter>
            </Card>

            <Card variant="elevated">
              <CardHeader>
                <CardTitle>Elevated card</CardTitle>
                <CardDescription>bg-4 surface · elevated shadow</CardDescription>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-fg-2">Card body content goes here.</p>
              </CardContent>
            </Card>

            <Card variant="flat">
              <CardHeader>
                <CardTitle>Flat card</CardTitle>
                <CardDescription>bg-2 surface · no shadow</CardDescription>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-fg-2">Card body content goes here.</p>
              </CardContent>
            </Card>

            <Card variant="ghost">
              <CardHeader>
                <CardTitle>Ghost card</CardTitle>
                <CardDescription>transparent · border only</CardDescription>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-fg-2">Card body content goes here.</p>
              </CardContent>
            </Card>
          </div>

          {/* Accent variants */}
          <div className="mt-3 grid grid-cols-4 gap-3">
            {(["blue", "green", "amber", "red"] as const).map((a) => (
              <Card key={a} variant="default" accent={a}>
                <CardContent className="pt-4">
                  <p className="font-mono text-[11px] text-fg-2">{a} accent</p>
                </CardContent>
              </Card>
            ))}
          </div>
        </Section>

        {/* ----------------------------------------------------------------- */}
        <Section title="Confidence Indicator">
          <div className="flex flex-col gap-2 max-w-[320px]">
            <ConfidenceIndicator score={96} label="Manufacturer" />
            <ConfidenceIndicator score={84} label="Part number" />
            <ConfidenceIndicator score={67} label="Model" />
            <ConfidenceIndicator score={42} label="Description" />
          </div>
        </Section>

        {/* ----------------------------------------------------------------- */}
        <Section title="Tier Headers">
          <div className="flex flex-col gap-1 rounded-card border border-hr-2 overflow-hidden">
            <TierHeader tier={1} count={3} />
            <div className="border-t border-hr-2" />
            <TierHeader tier={2} count={7} />
            <div className="border-t border-hr-2" />
            <TierHeader tier={3} count={0} />
          </div>
        </Section>

        {/* ----------------------------------------------------------------- */}
        <Section title="Run Summary Bar">
          <div className="rounded-card overflow-hidden border border-hr-2">
            <RunSummaryBar run={MOCK_RUN} showPhaseBar />
          </div>
          <div className="mt-2 rounded-card overflow-hidden border border-hr-2">
            <RunSummaryBar
              run={{ ...MOCK_RUN, urgency: "Stocking", warranty: "Active", amount: undefined }}
            />
          </div>
        </Section>

        {/* ----------------------------------------------------------------- */}
        <Section title="Asset Panel">
          <div className="max-w-[360px]">
            <AssetPanel specs={MOCK_SPECS} />
          </div>
        </Section>

        {/* ----------------------------------------------------------------- */}
        <Section title="CSS Patterns">
          <Row label="Leader rows">
            <div className="flex flex-col gap-2 w-64 bg-bg-3 rounded p-3 border border-hr-2">
              <div className="leader">
                <span className="lbl">Voltage</span>
                <span className="dots" />
                <span className="val">230/460V</span>
              </div>
              <div className="leader">
                <span className="lbl">HP</span>
                <span className="dots" />
                <span className="val">0.75</span>
              </div>
              <div className="leader">
                <span className="lbl">Frame</span>
                <span className="dots" />
                <span className="val">56J</span>
              </div>
            </div>
          </Row>

          <Row label="Section cap">
            <div className="w-64">
              <div className="section-cap">
                Tier 1 results
                <span className="rule" />
              </div>
            </div>
          </Row>

          <Row label="Stamp">
            <span className="stamp">✓ OEM Verified</span>
            <span className="stamp">✓ Price confirmed</span>
          </Row>

          <Row label="Placeholder">
            <div className="ph h-16 w-32 rounded text-[10px]">Photo</div>
          </Row>

          <Row label="Run ID chip">
            <div className="run-id">
              <span style={{ color: "var(--fg-4)", marginRight: 4 }}>RUN</span>
              ABC12345
            </div>
          </Row>
        </Section>

      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Layout helpers (local to this page)
// ---------------------------------------------------------------------------

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-4">
      <div className="section-cap">
        {title}
        <span className="rule" />
      </div>
      {children}
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-fg-4">{label}</span>
      <div className="flex flex-wrap items-center gap-2">{children}</div>
    </div>
  );
}
