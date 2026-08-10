import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { AnalysisReport } from '../../../types/analysis';
import { DecisionContext } from '../DecisionContext';

const buildReport = (overrides: {
  strategySynthesis?: Record<string, unknown>;
  disagreementExplanation?: Record<string, unknown>;
}): AnalysisReport => {
  const rawResult: Record<string, unknown> = {
    dashboard: {
      strategy_synthesis: overrides.strategySynthesis,
      agent_disagreement_explanation: overrides.disagreementExplanation,
    },
  };
  return {
    meta: {
      queryId: 'q-1',
      stockCode: '600519',
      stockName: '贵州茅台',
      reportType: 'detailed',
      createdAt: '2026-08-10T08:00:00Z',
    },
    summary: {
      analysisSummary: '趋势维持强势',
      operationAdvice: '买入',
      trendPrediction: '短线震荡偏强',
      sentimentScore: 78,
    },
    details: {
      rawResult,
    },
  } as unknown as AnalysisReport;
};

describe('DecisionContext', () => {
  it('does not render without strategy synthesis or risk control', () => {
    const { container } = render(
      <DecisionContext details={{ rawResult: { dashboard: {} } }} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders strategy synthesis layer with supporting/opposing strategies', () => {
    render(
      <DecisionContext
        details={{
          rawResult: {
            dashboard: {
              strategy_synthesis: {
                final_signal: 'buy',
                consensus_level: 'high',
                conflict_severity: 'medium',
                conflict_count: 2,
                confidence: 0.8,
                supporting_skills: [
                  { skill_id: 'bull_trend', signal: 'buy', confidence: 0.85 },
                ],
                opposing_skills: [
                  { skill_id: 'capital_heat', signal: 'hold', confidence: 0.4 },
                ],
              },
            },
          },
        }}
      />,
    );
    expect(screen.getByText('多策略综合')).toBeInTheDocument();
    expect(screen.getByText(/综合信号/)).toBeInTheDocument();
    expect(screen.getByText('buy')).toBeInTheDocument();
    expect(screen.getByText(/支持策略/)).toBeInTheDocument();
    expect(screen.getByText(/bull_trend/)).toBeInTheDocument();
    expect(screen.getByText(/反方策略/)).toBeInTheDocument();
    expect(screen.getByText(/capital_heat/)).toBeInTheDocument();
    expect(screen.getByText(/多策略综合为策略层共识/)).toBeInTheDocument();
  });

  it('renders risk downgrade transition from buy to sell', () => {
    render(
      <DecisionContext
        details={{
          rawResult: {
            dashboard: {
              agent_disagreement_explanation: {
                risk_control: {
                  applied: true,
                  from_signal: 'buy',
                  to_signal: 'hold',
                  post_risk_signal: 'hold',
                  trigger: 'risk_downgrade',
                },
                decision_path: 'risk_downgrade',
              },
            },
          },
        }}
      />,
    );
    expect(screen.getByText('风控下调')).toBeInTheDocument();
    expect(screen.getByText(/最终信号已由 buy 下调至 hold/)).toBeInTheDocument();
  });

  it('renders degraded warning when degraded events exist', () => {
    render(
      <DecisionContext
        details={{
          rawResult: {
            dashboard: {
              agent_disagreement_explanation: {
                degraded_events: [{ stage: 'skill', reason: 'timeout' }],
                risk_control: { applied: false, post_risk_signal: 'buy' },
              },
            },
          },
        }}
      />,
    );
    expect(screen.getByText(/数据降级/)).toBeInTheDocument();
  });
});
