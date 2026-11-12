"use client";

import {
  Bot,
  BrainCircuit,
  Check,
  Database,
  HardDriveDownload,
  KeyRound,
  Pencil,
  Plug,
  Plus,
  RefreshCw,
  Search,
  Server,
  Trash2,
  X
} from "lucide-react";
import { type FormEvent, useCallback, useMemo, useState } from "react";
import { api, type ModelKind, type ModelProfile } from "./api";
import { useApiResource } from "./hooks";
import { EmptyState, RequestState } from "./primitives";

type ModelForm = {
  name: string;
  kind: ModelKind;
  base_url: string;
  model_name: string;
  api_key: string;
  enabled: boolean;
};

const MODEL_TYPES: Array<{
  kind: ModelKind;
  label: string;
  detail: string;
  icon: typeof Bot;
}> = [
  { kind: "chat", label: "对话模型", detail: "知识编译、校验与问答", icon: Bot },
  { kind: "embedding", label: "向量模型", detail: "语义检索与文档索引", icon: Database },
  { kind: "reranker", label: "重排模型", detail: "优化检索结果排序", icon: Search }
];

const PURPOSES: Record<ModelKind, Array<{ value: string; label: string }>> = {
  chat: [
    { value: "compilation", label: "知识编译" },
    { value: "planning", label: "内容校验" },
    { value: "answer", label: "知识问答" }
  ],
  embedding: [{ value: "embedding", label: "向量生成" }],
  reranker: [{ value: "reranker", label: "结果重排" }]
};

function emptyForm(kind: ModelKind): ModelForm {
  return {
    name: "",
    kind,
    base_url: "",
    model_name: "",
    api_key: "",
    enabled: true
  };
}

export function ModelSettings({ showHeading = true }: { showHeading?: boolean } = {}) {
  const load = useCallback(async () => {
    const [profiles, assignments] = await Promise.all([
      api.listModelProfiles(),
      api.listModelAssignments()
    ]);
    return { profiles, assignments };
  }, []);
  const { data, error, loading, reload } = useApiResource(load);
  const loadIndexStatus = useCallback(() => api.getWikiIndexStatus(), []);
  const {
    data: indexStatus,
    error: indexStatusError,
    loading: indexStatusLoading,
    reload: reloadIndexStatus
  } = useApiResource(loadIndexStatus);
  const [activeKind, setActiveKind] = useState<ModelKind>("chat");
  const [form, setForm] = useState<ModelForm>(() => emptyForm("chat"));
  const [editingId, setEditingId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{
    healthy: boolean;
    message: string;
    latency_ms: number;
  } | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ModelProfile | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [assignmentError, setAssignmentError] = useState<string | null>(null);
  const [maintenanceAction, setMaintenanceAction] = useState<"wiki" | "raw" | null>(null);
  const [maintenanceMessage, setMaintenanceMessage] = useState<string | null>(null);
  const [maintenanceError, setMaintenanceError] = useState<string | null>(null);

  const visibleProfiles = useMemo(
    () => data?.profiles.filter((profile) => profile.kind === activeKind) ?? [],
    [activeKind, data?.profiles]
  );
  const assignmentMap = useMemo(
    () => new Map(data?.assignments.map((item) => [item.purpose, item.model_profile_id])),
    [data?.assignments]
  );

  function openCreate() {
    setEditingId(null);
    setForm(emptyForm(activeKind));
    setFormError(null);
    setTestResult(null);
    setShowForm(true);
  }

  function openEdit(profile: ModelProfile) {
    setEditingId(profile.id);
    setForm({
      name: profile.name,
      kind: profile.kind,
      base_url: profile.base_url,
      model_name: profile.model_name,
      api_key: "",
      enabled: profile.enabled
    });
    setFormError(null);
    setTestResult(null);
    setShowForm(true);
  }

  function updateForm(values: Partial<ModelForm>) {
    setForm((current) => ({ ...current, ...values }));
    setFormError(null);
    setTestResult(null);
  }

  async function testConnection() {
    setTesting(true);
    setFormError(null);
    setTestResult(null);
    try {
      setTestResult(
        await api.testModelConnection({
          profile_id: editingId,
          base_url: form.base_url,
          model_name: form.model_name,
          api_key: form.api_key || null
        })
      );
    } catch (cause) {
      setFormError(cause instanceof Error ? cause.message : "无法测试模型连接。");
    } finally {
      setTesting(false);
    }
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setFormError(null);
    try {
      if (editingId) {
        await api.updateModelProfile(editingId, {
          name: form.name,
          base_url: form.base_url,
          model_name: form.model_name,
          api_key: form.api_key || null,
          enabled: form.enabled
        });
      } else {
        await api.createModelProfile({
          name: form.name,
          kind: form.kind,
          base_url: form.base_url,
          model_name: form.model_name,
          api_key: form.api_key,
          enabled: form.enabled
        });
      }
      setShowForm(false);
      reload();
    } catch (cause) {
      setFormError(cause instanceof Error ? cause.message : "无法保存模型配置。");
    } finally {
      setSaving(false);
    }
  }

  async function assign(purpose: string, profileId: string) {
    setAssignmentError(null);
    try {
      await api.updateModelAssignment(purpose, profileId);
      reload();
      if (purpose === "embedding") {
        reloadIndexStatus();
        setMaintenanceMessage("向量模型已更新，请重建 Wiki 索引并回填原始资料向量。");
      }
    } catch (cause) {
      setAssignmentError(cause instanceof Error ? cause.message : "无法更新用途分配。");
    }
  }

  async function rebuildWikiIndex() {
    setMaintenanceAction("wiki");
    setMaintenanceError(null);
    setMaintenanceMessage(null);
    try {
      const result = await api.rebuildCurrentWikiIndex();
      setMaintenanceMessage(`Wiki 索引已重建至提交 ${result.commit}。`);
      reloadIndexStatus();
    } catch (cause) {
      setMaintenanceError(cause instanceof Error ? cause.message : "无法重建 Wiki 索引。");
    } finally {
      setMaintenanceAction(null);
    }
  }

  async function backfillRawEmbeddings() {
    setMaintenanceAction("raw");
    setMaintenanceError(null);
    setMaintenanceMessage(null);
    try {
      const run = await api.queueSourceEmbeddingBackfill();
      setMaintenanceMessage(`原始资料向量回填任务 ${run.id.slice(0, 8)} 已进入队列。`);
    } catch (cause) {
      setMaintenanceError(cause instanceof Error ? cause.message : "无法创建向量回填任务。");
    } finally {
      setMaintenanceAction(null);
    }
  }

  async function remove() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api.deleteModelProfile(deleteTarget.id);
      setDeleteTarget(null);
      reload();
    } catch (cause) {
      setFormError(cause instanceof Error ? cause.message : "无法删除模型配置。");
      setDeleteTarget(null);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="settings-page">
      {showHeading ? (
        <div className="settings-heading">
          <div className="settings-heading-icon"><BrainCircuit size={22} /></div>
          <div>
            <span className="eyebrow">AI 设置</span>
            <h1>模型配置</h1>
            <p>管理当前账号使用的模型服务与任务分工。</p>
          </div>
        </div>
      ) : null}

      <div className="settings-tabs" role="tablist" aria-label="模型类型">
        {MODEL_TYPES.map((item) => {
          const Icon = item.icon;
          const count = data?.profiles.filter((profile) => profile.kind === item.kind).length ?? 0;
          return (
            <button
              aria-selected={activeKind === item.kind}
              className={activeKind === item.kind ? "settings-tab settings-tab-active" : "settings-tab"}
              key={item.kind}
              onClick={() => {
                setActiveKind(item.kind);
                setShowForm(false);
              }}
              role="tab"
              type="button"
            >
              <Icon size={17} />
              <span><strong>{item.label}</strong><small>{item.detail}</small></span>
              <em>{count}</em>
            </button>
          );
        })}
      </div>

      {activeKind === "embedding" ? (
        <section className="settings-card">
          <div className="settings-card-head">
            <div>
              <h2>向量索引状态</h2>
              <p>模型分配或配置变化后，需要重建 Wiki 向量并回填原始资料向量。</p>
            </div>
          </div>
          <RequestState
            error={indexStatusError}
            loading={indexStatusLoading}
            onRetry={reloadIndexStatus}
          />
          {!indexStatusLoading && !indexStatusError && indexStatus ? (
            <div className="embedding-maintenance">
              <div className="embedding-maintenance-stats">
                <span>Wiki 向量 <strong>{indexStatus.wiki_vector_count}/{indexStatus.wiki_document_count}</strong></span>
                <span>Raw 向量 <strong>{indexStatus.raw_vector_count}/{indexStatus.raw_span_count}</strong></span>
              </div>
              {!indexStatus.assigned_embedding_profile_id ? (
                <div className="embedding-maintenance-notice">
                  请先在下方分配默认向量模型。
                </div>
              ) : (
                <>
                  {indexStatus.wiki_rebuild_required || indexStatus.raw_backfill_required ? (
                    <div className="embedding-maintenance-notice">
                      当前向量索引与已分配模型不完整或不一致，请执行对应维护操作。
                    </div>
                  ) : (
                    <div className="embedding-maintenance-success">
                      <Check size={15} /> 当前知识向量与已分配模型一致。
                    </div>
                  )}
                  <div className="embedding-maintenance-actions">
                    <button
                      className="button button-secondary"
                      disabled={maintenanceAction !== null || indexStatus.wiki_document_count === 0}
                      onClick={() => void rebuildWikiIndex()}
                      type="button"
                    >
                      <RefreshCw size={15} />
                      {maintenanceAction === "wiki" ? "正在重建…" : "重建 Wiki 索引"}
                    </button>
                    <button
                      className="button button-secondary"
                      disabled={maintenanceAction !== null || indexStatus.raw_span_count === 0}
                      onClick={() => void backfillRawEmbeddings()}
                      type="button"
                    >
                      <HardDriveDownload size={15} />
                      {maintenanceAction === "raw" ? "正在提交…" : "回填 Raw 向量"}
                    </button>
                  </div>
                </>
              )}
              {maintenanceError ? (
                <div className="settings-inline-error" role="alert">{maintenanceError}</div>
              ) : null}
              {maintenanceMessage ? (
                <div className="embedding-maintenance-message" role="status">{maintenanceMessage}</div>
              ) : null}
            </div>
          ) : null}
        </section>
      ) : null}

      <section className="settings-card">
        <div className="settings-card-head">
          <div>
            <h2>{MODEL_TYPES.find((item) => item.kind === activeKind)?.label}</h2>
            <p>统一使用 OpenAI 兼容协议，API 密钥加密保存。</p>
          </div>
          <button className="button" onClick={openCreate} type="button">
            <Plus size={15} /> 添加配置
          </button>
        </div>
        <RequestState error={error} loading={loading} onRetry={reload} />
        {!loading && !error && visibleProfiles.length === 0 ? (
          <EmptyState title="还没有配置" description="添加一个模型服务后即可用于自动编译、校验与知识问答。" />
        ) : (
          <div className="model-card-list">
            {visibleProfiles.map((profile) => {
              const purposes = data?.assignments
                .filter((item) => item.model_profile_id === profile.id)
                .map((item) => PURPOSES[profile.kind].find((purpose) => purpose.value === item.purpose)?.label)
                .filter(Boolean);
              return (
                <article className="model-config-card" key={profile.id}>
                  <div className="model-config-icon"><Server size={19} /></div>
                  <div className="model-config-main">
                    <div className="model-config-title">
                      <strong>{profile.name}</strong>
                      <span>OpenAI 兼容</span>
                      {profile.enabled ? <i><Check size={11} /> 已启用</i> : null}
                    </div>
                    <p>{profile.model_name}</p>
                    <small>{profile.base_url}</small>
                    {purposes?.length ? (
                      <div className="model-purpose-tags">
                        {purposes.map((purpose) => <span key={purpose}>{purpose}</span>)}
                      </div>
                    ) : null}
                  </div>
                  <div className="model-config-actions">
                    <button aria-label={`编辑 ${profile.name}`} onClick={() => openEdit(profile)} type="button"><Pencil size={15} /></button>
                    <button aria-label={`删除 ${profile.name}`} className="danger-icon" onClick={() => setDeleteTarget(profile)} type="button"><Trash2 size={15} /></button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      <section className="settings-card">
        <div className="settings-card-head">
          <div>
            <h2>用途分配</h2>
            <p>选择当前类型默认使用的模型。</p>
          </div>
        </div>
        <div className="assignment-list">
          {PURPOSES[activeKind].map((purpose) => (
            <label className="assignment-row" key={purpose.value}>
              <span><strong>{purpose.label}</strong><small>{purposeDescription(purpose.value)}</small></span>
              <select
                disabled={visibleProfiles.length === 0}
                onChange={(event) => void assign(purpose.value, event.target.value)}
                value={assignmentMap.get(purpose.value) ?? ""}
              >
                <option value="" disabled>选择模型</option>
                {visibleProfiles.filter((profile) => profile.enabled).map((profile) => (
                  <option key={profile.id} value={profile.id}>{profile.name}</option>
                ))}
              </select>
            </label>
          ))}
        </div>
        {assignmentError ? <div className="settings-inline-error">{assignmentError}</div> : null}
      </section>

      {showForm ? (
        <div className="settings-modal-backdrop" role="presentation" onMouseDown={() => setShowForm(false)}>
          <div aria-labelledby="model-form-title" aria-modal="true" className="settings-modal" onMouseDown={(event) => event.stopPropagation()} role="dialog">
            <div className="settings-modal-head">
              <div>
                <span className="eyebrow">{editingId ? "编辑配置" : "新增配置"}</span>
                <h2 id="model-form-title">{MODEL_TYPES.find((item) => item.kind === form.kind)?.label}</h2>
              </div>
              <button aria-label="关闭" onClick={() => setShowForm(false)} type="button"><X size={18} /></button>
            </div>
            <form className="settings-form" onSubmit={(event) => void save(event)}>
              <div className="model-protocol-notice">
                <Plug aria-hidden="true" size={16} />
                <span>服务需要实现 OpenAI 兼容的 <code>GET /models</code> 接口。</span>
              </div>
              <div className="settings-form-row">
                <label className="field">配置名称<input disabled={saving || testing} onChange={(event) => updateForm({ name: event.target.value })} placeholder="例如：默认对话模型" required value={form.name} /></label>
                <label className="field">模型名称<input disabled={saving || testing} onChange={(event) => updateForm({ model_name: event.target.value })} placeholder="例如：gpt-4.1-mini" required value={form.model_name} /></label>
              </div>
              <label className="field">API 地址<input disabled={saving || testing} onChange={(event) => updateForm({ base_url: event.target.value })} placeholder="https://api.example.com/v1" required type="url" value={form.base_url} /></label>
              <label className="field">
                <span className="field-label-with-icon"><KeyRound size={13} /> API 密钥</span>
                <input autoComplete="off" disabled={saving || testing} onChange={(event) => updateForm({ api_key: event.target.value })} placeholder={editingId ? "留空则使用已保存的密钥" : "输入 API Key"} required={!editingId} type="password" value={form.api_key} />
              </label>
              {testResult ? (
                <div
                  className={testResult.healthy ? "model-test-result model-test-success" : "model-test-result model-test-error"}
                  role={testResult.healthy ? "status" : "alert"}
                >
                  {testResult.healthy ? <Check size={15} /> : <X size={15} />}
                  <span>{testResult.message}</span>
                  <small>{testResult.latency_ms} ms</small>
                </div>
              ) : null}
              {formError ? <div className="settings-inline-error" role="alert">{formError}</div> : null}
              <div className="settings-modal-actions">
                <button className="button button-secondary" disabled={saving || testing} onClick={() => setShowForm(false)} type="button">取消</button>
                <button
                  className="button button-secondary"
                  disabled={
                    testing ||
                    saving ||
                    !form.base_url ||
                    !form.model_name ||
                    (!editingId && !form.api_key)
                  }
                  onClick={() => void testConnection()}
                  type="button"
                >
                  <Plug size={15} /> {testing ? "正在测试…" : "测试连接"}
                </button>
                <button className="button" disabled={saving || testing} type="submit">{saving ? "正在保存…" : "保存配置"}</button>
              </div>
            </form>
          </div>
        </div>
      ) : null}

      {deleteTarget ? (
        <div className="settings-modal-backdrop" role="presentation" onMouseDown={() => setDeleteTarget(null)}>
          <div aria-labelledby="delete-model-title" aria-modal="true" className="settings-modal settings-modal-small" onMouseDown={(event) => event.stopPropagation()} role="dialog">
            <div className="danger-dialog-icon"><Trash2 size={20} /></div>
            <h2 id="delete-model-title">删除模型配置？</h2>
            <p>“{deleteTarget.name}”及其用途分配将被删除，此操作无法撤销。</p>
            <div className="settings-modal-actions">
              <button className="button button-secondary" onClick={() => setDeleteTarget(null)} type="button">取消</button>
              <button className="button button-danger" disabled={deleting} onClick={() => void remove()} type="button">{deleting ? "正在删除…" : "确认删除"}</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function purposeDescription(purpose: string): string {
  const descriptions: Record<string, string> = {
    compilation: "把来源编译为结构化 Wiki",
    planning: "校验事实、引用和知识冲突",
    answer: "根据 Wiki 内容生成回答",
    embedding: "生成语义检索向量",
    reranker: "对候选结果进行二次排序"
  };
  return descriptions[purpose] ?? "";
}
