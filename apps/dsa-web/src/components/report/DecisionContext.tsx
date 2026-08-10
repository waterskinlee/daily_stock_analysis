import type React from 'react';
import type { ReportDetails as ReportDetailsType, ReportLanguage } from '../../types/analysis';
import { Badge, Card } from '../common';
import { DashboardPanelHeader } from '../dashboard';
import { getReportText, normalizeReportLanguage } from '../../utils/reportLanguage';

interface DecisionContextProps {
  details?: ReportDetailsType;
  language?: ReportLanguage;
}

type Signal = 'buy' | 'hold' | 'sell';

interface RiskControl {
  evidence_present?: boolean;
  override_enabled?: boolean;
  trigger?: string;
  applied?: boolean;
  reason?: string;
  post_risk_signal?: Signal;
  from_signal?: Signal | null;
  to_signal?: Signal | null;
}

interface StrategyItem {
  skill_id?: string;
  signal?: Signal;
  confidence?: number;
}

interface StrategySynthesis {
  final_signal?: Signal | 'N/A';
  consensus_level?: string;
  conflict_severity?: string;
  conflict_count?: number;
  confidence?: number;
  supporting_skills?: StrategyItem[];
  opposing_skills?: StrategyItem[];
}

interface DecisionExplanation {
  base_disagreement?: {
    type?: string;
    agents?: Array<{ agent?: string; signal?: Signal; confidence?: number }>;
  };
  risk_control?: RiskControl;
  pipeline_start_action?: string;
  final_action?: string;
  final_adjustments?: Array<{ from_action: string; to_action: string; source: string }>;
  decision_path?: string;
  degraded_events?: Array<{ stage: string; reason: string }>;
}

const SIGNAL_LABEL: Record<Signal, string> = { buy: 'buy', hold: 'hold', sell: 'sell' };

const asMapping = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' ? (value as Record<string, unknown>) : {};

const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

const numberPct = (value: unknown): string | null => {
  if (typeof value !== 'number') return null;
  return `${(value * 100).toFixed(0)}%`;
};

/**
 * 决策上下文卡 - 展示"策略层综合信号 → 风险/降级 → 最终决策"的路径，
 * 解释为什么策略综合看多但最终结论偏空。
 */
export const DecisionContext: React.FC<DecisionContextProps> = ({
  details,
  language = 'zh',
}) => {
  const reportLanguage = normalizeReportLanguage(language);
  const text = getReportText(reportLanguage);

  const rawResult = asMapping(details?.rawResult);
  const dashboard = asMapping(rawResult.dashboard);
  const explanation = asMapping(dashboard.agent_disagreement_explanation) as DecisionExplanation;
  const synthesis = asMapping(dashboard.strategy_synthesis) as StrategySynthesis;

  const strategyLayerPresent = Boolean(
    synthesis.final_signal && synthesis.final_signal !== 'N/A') || Boolean(synthesis.confidence);
  const riskControl = explanation?.risk_control || {};
  const riskApplied = Boolean(riskControl.applied);
  const finalAdjustments = asArray(explanation?.final_adjustments);
  const degraded = Boolean(asArray(explanation?.degraded_events).length);

  if (!strategyLayerPresent && !riskApplied && finalAdjustments.length === 0 && !degraded) {
    return null;
  }

  const consensusText = synthesis.consensus_level || text.unknown;
  const conflictText = synthConflictText(synthesis.conflict_severity, synthesis.conflict_count);
  const riskFrom = riskControl.from_signal ? SIGNAL_LABEL[riskControl.from_signal] : null;
  const riskTo = riskControl.to_signal ? SIGNAL_LABEL[riskControl.to_signal] : null;

  const signalBadge = (value: Signal | undefined): React.ReactNode | null => {
    if (!value) return null;
    const tone: 'success' | 'danger' | 'warning' =
      value === 'buy' ? 'success' : value === 'sell' ? 'danger' : 'warning';
    return <Badge variant={tone} className="shadow-none">{SIGNAL_LABEL[value]}</Badge>;
  };

  return (
    <Card variant="bordered" padding="md" className="home-panel-card text-left">
      <DashboardPanelHeader
        eyebrow={text.decisionContext}
        title={text.decisionContextSubtitle}
        className="mb-3"
      />

      <div className="space-y-3 text-sm leading-6">
        {/* 层级说明 */}
        <div className="home-subpanel p-3 text-xs text-muted-text">
          <span className="font-medium text-foreground">{text.strategyLayer} → {text.finalDecisionLayer}: </span>
          {text.layeringNote}
        </div>

        {strategyLayerPresent && (
          <div className="home-subpanel p-3">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <span className="label-uppercase">{text.strategySynthesis}</span>
              {synthesis.final_signal && synthesis.final_signal !== 'N/A' && (
                <span className="ml-1 flex items-center gap-1">
                  {text.strategyFinalSignal}:
                  {signalBadge(synthesis.final_signal)}
                </span>
              )}
              {synthesis.consensus_level ? (
                <span className="home-accent-chip px-2 py-0.5 text-xs">
                  {text.strategyConsensus}: {consensusText}
                </span>
              ) : null}
              {conflictText ? (
                <span className="home-accent-chip px-2 py-0.5 text-xs">
                  {text.strategyConflict}: {conflictText}
                </span>
              ) : null}
              {synthesis.confidence ? (
                <span className="home-accent-chip px-2 py-0.5 text-xs">
                  {text.strategyConfidence}: {numberPct(synthesis.confidence)}
                </span>
              ) : null}
            </div>
            {(asArray(synthesis.supporting_skills).length > 0 || asArray(synthesis.opposing_skills).length > 0) && (
              <div className="mt-2 grid grid-cols-1 gap-1 md:grid-cols-2 text-xs">
                {asArray(synthesis.supporting_skills).length > 0 && (
                  <div className="text-success">
                    <span className="font-medium">{text.supportingStrategies}: </span>
                    {formatStrategyItems(asArray(synthesis.supporting_skills), reportLanguage)}
                  </div>
                )}
                {asArray(synthesis.opposing_skills).length > 0 && (
                  <div className="text-danger">
                    <span className="font-medium">{text.opposingStrategies}: </span>
                    {formatStrategyItems(asArray(synthesis.opposing_skills), reportLanguage)}
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* 风控降级 */}
        {riskApplied && (
          <div className="home-subpanel p-3">
            <div className="mb-1 flex flex-wrap items-center gap-2">
              <Badge variant="danger" className="shadow-none">{text.riskDowngrade}</Badge>
              {signalBadge(riskControl.from_signal ?? undefined)} <span>→</span> {signalBadge(riskControl.to_signal ?? undefined)}
            </div>
            {riskFrom && riskTo && (
              <p className="text-sm text-foreground">
                {templatedRiskDetail(text.riskDowngradeDetail, riskFrom, riskTo)}
              </p>
            )}
          </div>
        )}

        {/* 数据降级 */}
        {degraded && (
          <div className="home-subpanel p-3 text-xs text-warning">
            <span className="font-medium">{text.degradedWarning}</span>
          </div>
        )}
      </div>
    </Card>
  );
};

const formatStrategyItems = (items: unknown[], language: ReportLanguage): string => {
  const labels = getReportText(language);
  const parts = items.map((item) => {
    const record = asMapping(item);
    const name = String(record.skill_id || labels.unknown);
    const sig = record.signal as Signal | undefined;
    const conf = numberPct(record.confidence);
    const suffix = sig ? `/${SIGNAL_LABEL[sig]}${conf ? ` ${conf}` : ''}` : conf ? ` ${conf}` : '';
    return `${name}${suffix}`;
  });
  return parts.join('、');
};

const synthConflictText = (severity: unknown, count: unknown): string => {
  const sev = String(severity || '').trim();
  const cnt = typeof count === 'number' ? count : 0;
  if (!sev && cnt <= 0) return '';
  return `${sev}(${cnt})`;
};

const templatedRiskDetail = (template: string, from: string, to: string): string =>
  template.replace('{from}', from).replace('{to}', to);