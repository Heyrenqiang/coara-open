import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Empty,
  InputNumber,
  Progress,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { BarChartOutlined, CheckOutlined, ReloadOutlined } from "@ant-design/icons";
import { PageShell } from "../components/layout/PageShell";
import { PageHeader } from "../components/layout/PageHeader";
import { useStore } from "../lib/store";
import {
  fetchUsageDashboard,
  fetchUsagePricing,
  updateUsagePricing,
  type UsageAgentKindRow,
  type UsageDashboardResponse,
  type UsageDayRow,
  type UsageModelRow,
  type UsagePricingEntry,
  type UsageTokenTotals,
  type UsageWorkspaceRow,
} from "../lib/api";

const { Text } = Typography;

const DAY_OPTIONS = [
  { value: 1, label: "最近 1 天" },
  { value: 7, label: "最近 7 天" },
  { value: 30, label: "最近 30 天" },
  { value: 90, label: "最近 90 天" },
];

// 单源原则：金额 / 词元计数 / 命中率 / 费用状态均由后端 usage_display.py 算好下发
// （*_display / cache_hit_pct / cache_hit_level / cost_state），本页只渲染不格式化。

const MONEY_BLUE = "var(--coara-accent)";
const HIT_GREEN = "var(--coara-success)";

function hitRateColumn<T extends UsageTokenTotals>(): ColumnsType<T>[number] {
  return {
    title: "缓存命中率",
    dataIndex: "cache_hit_pct",
    key: "hit",
    width: 150,
    render: (_: number, row: T) => (
      <Progress
        percent={row.cache_hit_pct ?? 0}
        size="small"
        format={() => row.cache_hit_display ?? "—"}
        strokeColor={row.cache_hit_level === "good" ? HIT_GREEN : MONEY_BLUE}
      />
    ),
  };
}

function costColumn<T extends UsageTokenTotals>(): ColumnsType<T>[number] {
  return {
    title: "费用",
    dataIndex: "cost_total",
    key: "cost",
    align: "right",
    width: 120,
    sorter: (a: T, b: T) => Number(a.cost_total || 0) - Number(b.cost_total || 0),
    render: (_: number, row: T) => {
      if (row.cost_state === "priced") {
        return <span style={{ color: MONEY_BLUE, fontWeight: 500 }}>{row.cost_total_display}</span>;
      }
      if (row.cost_state === "unpriced") return <Tag style={{ fontSize: 11 }}>未配价</Tag>;
      return <Text type="secondary">{row.cost_total_display ?? "¥0"}</Text>;
    },
  };
}

function tokenColumns<T extends UsageTokenTotals>(
  nameTitle: string,
  nameKey: keyof T & string,
): ColumnsType<T> {
  return [
    { title: nameTitle, dataIndex: nameKey, key: nameKey, ellipsis: true },
    {
      title: "总输入",
      dataIndex: "input_display",
      key: "input",
      align: "right",
      width: 110,
    },
    {
      title: "缓存命中输入",
      dataIndex: "cache_read_display",
      key: "cache",
      align: "right",
      width: 120,
    },
    {
      title: "输出",
      dataIndex: "output_display",
      key: "output",
      align: "right",
      width: 100,
    },
    hitRateColumn<T>(),
    costColumn<T>(),
  ];
}

/** 与用量空间对话浮窗：仅当 web 视图已切到用量空间（侧边栏点「用量」触发）时挂载。 */
const ModuleChatFloat = lazy(() =>
  import("../features/chat/ModuleChatFloat").then((m) => ({ default: m.ModuleChatFloat })),
);

/** 用量空间的展示名（侧边栏 home: 分支即按它找 home_view）。 */
const USAGE_SPACE_NAME = "用量";

export function UsageView() {
  const activeName = useStore((s) => s.activeName);
  const usageViewActive = activeName === USAGE_SPACE_NAME;
  const [data, setData] = useState<UsageDashboardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(7);
  const [workspaceId, setWorkspaceId] = useState<string>("");

  const [pricing, setPricing] = useState<UsagePricingEntry[]>([]);
  const [pricingLoading, setPricingLoading] = useState(false);
  const [pricingMsg, setPricingMsg] = useState<string | null>(null);

  const loadPricing = useCallback(async () => {
    setPricingLoading(true);
    try {
      const res = await fetchUsagePricing();
      setPricing(res.models);
      setPricingMsg(null);
    } catch (err) {
      setPricingMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setPricingLoading(false);
    }
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchUsageDashboard({
        days,
        workspaceId: workspaceId || undefined,
      });
      setData(res);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [days, workspaceId]);

  useEffect(() => {
    void load();
    void loadPricing();
  }, [load, loadPricing]);

  const workspaceOptions = useMemo(() => {
    const opts = [{ value: "", label: "全部工作空间" }];
    for (const ws of data?.workspaces || []) {
      opts.push({ value: ws.id, label: ws.name || ws.id });
    }
    return opts;
  }, [data?.workspaces]);

  const totals = data?.totals;
  const hitPct = totals?.cache_hit_pct ?? 0;
  const hitLevel = totals?.cache_hit_level;

  const header = (
    <PageHeader
      title={
        <>
          <BarChartOutlined style={{ marginRight: 8 }} />
          Token 用量与费用
        </>
      }
      subline={
        <Text type="secondary" style={{ fontSize: 12 }}>
          费用按模型价格估算。命中率 = 缓存命中 ÷ 总输入。含主会话与子智能体，不含工作流引擎与压缩。
        </Text>
      }
      actions={
        <Space wrap>
          <Select value={days} options={DAY_OPTIONS} style={{ width: 140 }} onChange={setDays} />
          <Select
            value={workspaceId}
            options={workspaceOptions}
            style={{ minWidth: 180 }}
            onChange={setWorkspaceId}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
        </Space>
      }
    />
  );

  return (
    <PageShell header={header} surface="subtle" padded={false}>
      <div style={{ padding: 24, maxWidth: 1100, margin: "0 auto" }}>
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          {error && <Alert type="error" showIcon message={error} />}

          <Spin spinning={loading}>
            {!data && !loading ? (
              <Empty description="暂无用量数据" />
            ) : data ? (
              <Space direction="vertical" size={16} style={{ width: "100%" }}>
                <Text type="secondary">
                  时间窗：{data.window.from_display || "—"} — {data.window.to_display || "—"}
                </Text>

                <Card size="small">
                  {/* 英雄位：总费用一行压场；命中率/ token 量/费用构成逐级降为配角 */}
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 24, flexWrap: "wrap" }}>
                    <div>
                      <Text type="secondary">总费用（估算）</Text>
                      <div style={{ fontSize: 34, fontWeight: 700, color: MONEY_BLUE, lineHeight: 1.2 }}>
                        {totals?.cost_total_display}
                      </div>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        非命中 {totals?.cost_miss_display} · 命中{" "}
                        <span style={{ color: HIT_GREEN }}>{totals?.cost_hit_display}</span> · 输出{" "}
                        {totals?.cost_out_display}
                      </Text>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 20, flexWrap: "wrap" }}>
                      <Progress
                        type="dashboard"
                        percent={hitPct}
                        size={96}
                        format={() => totals?.cache_hit_display ?? "—"}
                        strokeColor={hitLevel === "good" ? HIT_GREEN : MONEY_BLUE}
                      />
                      <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
                        <span>
                          <Text type="secondary" style={{ fontSize: 12 }}>总输入</Text>
                          <div style={{ fontWeight: 600 }}>{totals?.input_display}</div>
                        </span>
                        <span>
                          <Text type="secondary" style={{ fontSize: 12 }}>缓存命中</Text>
                          <div style={{ fontWeight: 600 }}>{totals?.cache_read_display}</div>
                        </span>
                        <span>
                          <Text type="secondary" style={{ fontSize: 12 }}>总输出</Text>
                          <div style={{ fontWeight: 600 }}>{totals?.output_display}</div>
                        </span>
                      </div>
                    </div>
                  </div>
                </Card>

                <PricingCard
                  models={pricing}
                  loading={pricingLoading}
                  message={pricingMsg}
                  onSaved={() => {
                    void loadPricing();
                    void load();
                  }}
                />

                <Card title="按调用方（子智能体）" size="small">
                  <Table<UsageAgentKindRow>
                    size="small"
                    rowKey="agent_kind"
                    pagination={false}
                    columns={tokenColumns<UsageAgentKindRow>("类型", "label")}
                    dataSource={data.by_agent_kind}
                    locale={{ emptyText: "该时间范围内没有 LLM 用量" }}
                  />
                </Card>

                <Card title="按模型" size="small">
                  <Table<UsageModelRow>
                    size="small"
                    rowKey="model_key"
                    pagination={false}
                    columns={tokenColumns<UsageModelRow>("模型", "label")}
                    dataSource={data.by_model}
                    locale={{ emptyText: "该时间范围内没有 LLM 用量" }}
                  />
                </Card>

                <Card title="按日" size="small">
                  <Table<UsageDayRow>
                    size="small"
                    rowKey="day"
                    pagination={false}
                    columns={tokenColumns<UsageDayRow>("日期", "day")}
                    dataSource={data.by_day}
                    locale={{ emptyText: "该时间范围内没有 LLM 用量" }}
                  />
                </Card>

                <Card title="按工作空间" size="small">
                  <Table<UsageWorkspaceRow>
                    size="small"
                    rowKey="workspace_id"
                    pagination={false}
                    columns={tokenColumns<UsageWorkspaceRow>("工作空间", "workspace_name")}
                    dataSource={data.by_workspace}
                    locale={{ emptyText: "该时间范围内没有 LLM 用量" }}
                  />
                </Card>
              </Space>
            ) : null}
          </Spin>
        </Space>
        </div>
      {usageViewActive && (
        <Suspense fallback={null}>
          <ModuleChatFloat
            subject="usage"
            title="与用量助手对话"
            emptyHint="问用量：本月各模型费用、命中率走势、异常消耗排查"
          />
        </Suspense>
      )}
    </PageShell>
  );
}

function PricingCard({
  models,
  loading,
  message,
  onSaved,
}: {
  models: UsagePricingEntry[];
  loading: boolean;
  message: string | null;
  onSaved: () => void;
}) {
  const [values, setValues] = useState<Record<string, Record<string, number>>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);

  useEffect(() => {
    const init: Record<string, Record<string, number>> = {};
    for (const m of models) {
      const p = m.pricing || {};
      init[m.model_key] = {
        input: p.input ?? 0,
        cache_hit: p.cache_hit ?? 0,
        output: p.output ?? 0,
      };
    }
    setValues(init);
  }, [models]);

  const updateField = (key: string, field: string, val: number | null) => {
    setValues((prev) => {
      const cur = prev[key] || { input: 0, cache_hit: 0, output: 0 };
      const next = { ...cur, [field]: val ?? 0 };
      return { ...prev, [key]: next };
    });
  };

  const splitKey = (modelKey: string) => {
    const [provider, ...rest] = modelKey.split("/");
    return { provider, model: rest.join("/") };
  };

  const saveEntry = async (m: UsagePricingEntry) => {
    const v = values[m.model_key];
    if (!v) return;
    setSavingKey(m.model_key);
    const { provider, model } = splitKey(m.model_key);
    try {
      await updateUsagePricing({
        provider,
        model,
        pricing: {
          input: v.input,
          cache_hit: v.cache_hit,
          output: v.output,
        },
      });
    } catch {
      // 错误通过 message 展现
    } finally {
      setSavingKey(null);
      onSaved();
    }
  };

  const columns: ColumnsType<UsagePricingEntry> = [
    {
      title: "模型",
      dataIndex: "model_key",
      key: "model",
      ellipsis: true,
      render: (_, m) => <span>{m.model_key}</span>,
    },
    {
      title: "输入",
      key: "input",
      width: 110,
      render: (_, m) => (
        <InputNumber
          size="small"
          controls={false}
          value={values[m.model_key]?.input ?? 0}
          onChange={(v) => updateField(m.model_key, "input", v)}
          style={{ width: "100%" }}
        />
      ),
    },
    {
      title: "缓存命中",
      key: "cache_hit",
      width: 110,
      render: (_, m) => (
        <InputNumber
          size="small"
          controls={false}
          value={values[m.model_key]?.cache_hit ?? 0}
          onChange={(v) => updateField(m.model_key, "cache_hit", v)}
          style={{ width: "100%" }}
        />
      ),
    },
    {
      title: "输出",
      key: "output",
      width: 110,
      render: (_, m) => (
        <InputNumber
          size="small"
          controls={false}
          value={values[m.model_key]?.output ?? 0}
          onChange={(v) => updateField(m.model_key, "output", v)}
          style={{ width: "100%" }}
        />
      ),
    },
    {
      title: "操作",
      key: "op",
      width: 48,
      render: (_, m) => (
        <Button
          size="small"
          type="text"
          loading={savingKey === m.model_key}
          onClick={() => saveEntry(m)}
          icon={<CheckOutlined />}
          title="保存"
        />
      ),
    },
  ];

  // 低频设置项：默认折叠，避免挤占看板主信息区；展开后才展示编辑表
  return (
    <Collapse
      items={[
        {
          key: "pricing",
          label: <span style={{ fontWeight: 500 }}>模型价格</span>,
          children: (
            <>
              {message && <Alert type="warning" showIcon message={message} style={{ marginBottom: 8 }} />}
              <Table<UsagePricingEntry>
                size="small"
                rowKey="model_key"
                pagination={false}
                columns={columns}
                dataSource={models}
                loading={loading}
                locale={{ emptyText: "暂无可配置的模型" }}
              />
            </>
          ),
        },
      ]}
    />
  );
}
