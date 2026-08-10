"use client";

import { Alert, Button, Chip, Modal, Tabs, TextArea, toast } from "@heroui/react";
import { Check, Database, MapPinned, RefreshCw, Route, ShieldAlert, X } from "lucide-react";
import Image from "next/image";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRealtimeRefresh } from "./use-realtime-refresh";

type ReportStatus = "pending" | "approved" | "rejected";
type TaskStatus = "open" | "claimed" | "submitted" | "verified" | "cancelled";
type ReviewDialog =
  | { kind: "report"; item: VolunteerReport; action: "approve" | "reject" }
  | { kind: "task"; item: VolunteerTask; action: "verify" | "return" | "cancel" | "reopen" };

type VolunteerReport = {
  id: string;
  categoryLabel: string;
  cleanupReasonLabel: string;
  description: string;
  address: string;
  lat: number;
  lng: number;
  photoUrl: string;
  status: ReportStatus;
  priority: "low" | "normal" | "urgent";
  reviewNote: string;
  analysisStatus: string;
  qualityScore: number | null;
  analysisSummary: string;
  createdAt: string;
  reporter: { email: string; displayName: string };
};

type VolunteerTask = {
  id: string;
  obstacleId: string;
  title: string;
  description: string;
  categoryLabel: string;
  address: string;
  priority: "low" | "normal" | "urgent";
  status: TaskStatus;
  assigneeId?: string;
  assigneeName?: string;
  assigneeEmail?: string;
  completionNote: string;
  reviewNote: string;
  photoUrl: string;
  updatedAt: string;
};

type OperationsSummary = {
  reports: Partial<Record<ReportStatus, number>>;
  tasks: Partial<Record<TaskStatus, number>>;
  obstacles: Record<string, number>;
  consistent: boolean;
  issueCount: number;
  generatedAt: string;
};

const reportLabels: Record<ReportStatus, string> = { pending: "待审核", approved: "已通过", rejected: "已驳回" };
const taskLabels: Record<TaskStatus, string> = { open: "待认领", claimed: "已认领", submitted: "待复核", verified: "已完成", cancelled: "已取消" };

async function adminJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    cache: "no-store",
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  const body = await response.json().catch(() => null) as { detail?: string } | null;
  if (!response.ok) throw new Error(body?.detail || `HTTP ${response.status}`);
  return body as T;
}

export function VolunteerAdminView() {
  const [tab, setTab] = useState<"reports" | "tasks">("reports");
  const [reportStatus, setReportStatus] = useState<ReportStatus>("pending");
  const [taskStatus, setTaskStatus] = useState<"all" | TaskStatus>("all");
  const [reports, setReports] = useState<Record<ReportStatus, VolunteerReport[]>>({ pending: [], approved: [], rejected: [] });
  const [tasks, setTasks] = useState<VolunteerTask[]>([]);
  const [summary, setSummary] = useState<OperationsSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [reviewDialog, setReviewDialog] = useState<ReviewDialog | null>(null);
  const [reviewNote, setReviewNote] = useState("");
  const [actionPending, setActionPending] = useState(false);
  const loadSequence = useRef(0);

  const load = useCallback(async (silent = false) => {
    const sequence = ++loadSequence.current;
    if (!silent) setLoading(true);
    if (!silent) setErrors([]);
    const results = await Promise.allSettled([
      adminJson<{ items: VolunteerReport[] }>("/api/v1/admin/reports?status=pending"),
      adminJson<{ items: VolunteerReport[] }>("/api/v1/admin/reports?status=approved"),
      adminJson<{ items: VolunteerReport[] }>("/api/v1/admin/reports?status=rejected"),
      adminJson<{ items: VolunteerTask[] }>("/api/v1/admin/tasks"),
      adminJson<OperationsSummary>("/api/v1/admin/operations/summary"),
    ]);
    const names = ["待审核上报", "已通过上报", "已驳回上报", "公共任务", "一致性摘要"];
    const nextErrors: string[] = [];
    results.forEach((result, index) => {
      if (result.status === "rejected") nextErrors.push(`${names[index]}加载失败：${result.reason instanceof Error ? result.reason.message : "未知错误"}`);
    });
    if (sequence !== loadSequence.current) return;
    const [pendingResult, approvedResult, rejectedResult, taskResult, summaryResult] = results;
    if (pendingResult.status === "fulfilled") setReports((value) => ({ ...value, pending: pendingResult.value.items }));
    if (approvedResult.status === "fulfilled") setReports((value) => ({ ...value, approved: approvedResult.value.items }));
    if (rejectedResult.status === "fulfilled") setReports((value) => ({ ...value, rejected: rejectedResult.value.items }));
    if (taskResult.status === "fulfilled") setTasks(taskResult.value.items);
    if (summaryResult.status === "fulfilled") setSummary(summaryResult.value);
    setErrors(nextErrors);
    setLoading(false);
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    const timer = window.setInterval(() => void load(true), 30000);
    const onVisibility = () => { if (document.visibilityState === "visible") void load(true); };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", onVisibility);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", onVisibility);
    };
  }, [load]);
  const realtimeLoad = useCallback(() => { void load(true); }, [load]);
  useRealtimeRefresh(realtimeLoad);

  const reviewReport = async (report: VolunteerReport, action: "approve" | "reject", note: string) => {
    if (action === "reject" && note.trim().length < 2) return;
    try {
      await adminJson(`/api/v1/admin/reports/${encodeURIComponent(report.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ action, note, publishTask: action === "approve", priority: report.priority }),
      });
      await load();
      toast.success(action === "approve" ? "上报已通过并生成公共任务" : "上报已驳回");
    } catch (cause) {
      const message = `审核提交失败：${cause instanceof Error ? cause.message : "请稍后重试"}`;
      setErrors([message]);
      toast.danger("审核提交失败", { description: message });
      throw cause;
    }
  };

  const reviewTask = async (task: VolunteerTask, action: "verify" | "return" | "cancel" | "reopen", note: string) => {
    if ((action === "return" || action === "cancel") && note.trim().length < 2) return;
    try {
      await adminJson(`/api/v1/admin/tasks/${encodeURIComponent(task.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ action, note }),
      });
      await load();
      toast.success(({ verify: "任务已确认闭环", return: "任务已退回", cancel: "派单已取消", reopen: "任务已重新发布" })[action]);
    } catch (cause) {
      const message = `任务操作失败：${cause instanceof Error ? cause.message : "请检查当前状态"}`;
      setErrors([message]);
      toast.danger("任务操作失败", { description: message });
      throw cause;
    }
  };

  const openReview = (dialog: ReviewDialog) => {
    setReviewDialog(dialog);
    setReviewNote("");
  };

  const closeReview = () => {
    if (actionPending) return;
    setReviewDialog(null);
    setReviewNote("");
  };

  const submitReview = async () => {
    if (!reviewDialog) return;
    const note = reviewNote.trim();
    const requiresReason = reviewDialog.action === "reject" || reviewDialog.action === "return" || reviewDialog.action === "cancel";
    if (requiresReason && note.length < 2) return;
    setActionPending(true);
    try {
      if (reviewDialog.kind === "report") await reviewReport(reviewDialog.item, reviewDialog.action, note);
      else await reviewTask(reviewDialog.item, reviewDialog.action, note);
      setReviewDialog(null);
      setReviewNote("");
    } catch {
      // The action-specific handler keeps the modal open and reports the error.
    } finally {
      setActionPending(false);
    }
  };

  const reportCounts = {
    pending: summary?.reports.pending ?? reports.pending.length,
    approved: summary?.reports.approved ?? reports.approved.length,
    rejected: summary?.reports.rejected ?? reports.rejected.length,
  };
  const taskCounts = {
    open: summary?.tasks.open ?? tasks.filter((item) => item.status === "open").length,
    claimed: summary?.tasks.claimed ?? tasks.filter((item) => item.status === "claimed").length,
    submitted: summary?.tasks.submitted ?? tasks.filter((item) => item.status === "submitted").length,
    verified: summary?.tasks.verified ?? tasks.filter((item) => item.status === "verified").length,
    cancelled: summary?.tasks.cancelled ?? tasks.filter((item) => item.status === "cancelled").length,
  };
  const taskTotal = Object.values(taskCounts).reduce((total, count) => total + count, 0);
  const visibleTasks = useMemo(() => taskStatus === "all" ? tasks : tasks.filter((item) => item.status === taskStatus), [taskStatus, tasks]);
  const actionNeedsReason = reviewDialog?.action === "reject" || reviewDialog?.action === "return" || reviewDialog?.action === "cancel";
  const actionLabels = { approve: "通过并派单", reject: "驳回上报", verify: "确认闭环", return: "退回任务", cancel: "取消派单", reopen: "重新发布" };

  return <div className="subpage admin-page">
    <div className="subpage-head"><div><span className="eyebrow">VOLUNTEER OPERATIONS</span><h1>志愿者审核与公共派单</h1><p>手机上报、审核入库、地图障碍和 App 公共任务使用同一条状态链。</p></div><div className="admin-head-actions"><Chip className="architecture-badge" size="sm" variant="soft"><ShieldAlert size={15} />管理员认证保护</Chip><Button className="secondary-action" size="sm" variant="ghost" onClick={() => void load()} isDisabled={loading}><RefreshCw size={15} className={loading ? "spin" : ""} />刷新</Button></div></div>

    <Alert className="admin-source-note panel" status="default"><Alert.Indicator><Database size={16} /></Alert.Indicator><Alert.Content><Alert.Title>数据口径</Alert.Title><Alert.Description>监管首页“活动事件”包含边缘设备识别事件；这里“公共派单”只统计审核通过并发布给志愿者的任务，两者不是同一个指标。</Alert.Description></Alert.Content></Alert>
    <div className="admin-summary-grid">
      <div className="panel"><span>待审核上报</span><strong>{reportCounts.pending}</strong></div>
      <div className="panel"><span>待认领任务</span><strong>{taskCounts.open}</strong></div>
      <div className="panel"><span>处理中</span><strong>{taskCounts.claimed + taskCounts.submitted}</strong></div>
      <div className={`panel consistency-${summary?.consistent === false ? "bad" : "good"}`}><span>状态链一致性</span><strong>{summary ? (summary.consistent ? "正常" : `${summary.issueCount} 项异常`) : "检查中"}</strong></div>
    </div>

    <Tabs className="admin-tabs" selectedKey={tab} onSelectionChange={(key) => setTab(String(key) as "reports" | "tasks")} aria-label="志愿者协同视图"><Tabs.ListContainer><Tabs.List><Tabs.Tab id="reports">上报审核 <em>{reportCounts.pending}</em></Tabs.Tab><Tabs.Tab id="tasks">公共派单 <em>{taskTotal}</em></Tabs.Tab></Tabs.List></Tabs.ListContainer></Tabs>
    {errors.map((error) => <Alert className="admin-error panel" status="danger" key={error}><Alert.Indicator><ShieldAlert size={15} /></Alert.Indicator><Alert.Content><Alert.Title>数据加载异常</Alert.Title><Alert.Description>{error}</Alert.Description></Alert.Content></Alert>)}

    {tab === "reports" && <>
      <div className="filter-row admin-filters">{(["pending", "approved", "rejected"] as const).map((status) => <button key={status} className={reportStatus === status ? "active" : ""} onClick={() => setReportStatus(status)}>{reportLabels[status]} {reportCounts[status]}</button>)}</div>
      <div className="review-list">
        {!loading && reports[reportStatus].length === 0 && <div className="panel admin-empty"><Check size={24} /><strong>当前没有{reportLabels[reportStatus]}上报</strong><span>新的手机上报会自动出现在这里。</span></div>}
        {reports[reportStatus].map((report) => <article className="panel review-card" key={report.id}>
          <Image className="review-photo" src={report.photoUrl} alt={report.categoryLabel} width={320} height={200} unoptimized />
          <div className="review-copy"><div className="review-title"><Chip className={`priority priority-${report.priority}`} size="sm" variant="soft">{({ low: "低", normal: "普通", urgent: "紧急" })[report.priority]}</Chip><h2>{report.categoryLabel}</h2><small>{new Date(report.createdAt).toLocaleString("zh-CN")}</small></div><p>{report.description}</p><div className="review-meta"><span><MapPinned size={14} />{report.address}</span><span><ShieldAlert size={14} />{report.cleanupReasonLabel}</span><span><Database size={14} />质量评分：{report.qualityScore === null ? "待计算" : `${Math.round(report.qualityScore * 100)} 分`} · {report.analysisStatus}</span><span>上报人：{report.reporter.displayName}（{report.reporter.email}）</span><span>编号：{report.id}</span></div>{report.analysisSummary && <div className="review-note">数据分析：{report.analysisSummary}</div>}{report.reviewNote && <div className="review-note">审核说明：{report.reviewNote}</div>}</div>
          {report.status === "pending" && <div className="review-actions"><Button className="secondary-action danger-action" size="sm" variant="ghost" onClick={() => openReview({ kind: "report", item: report, action: "reject" })}><X size={15} />驳回</Button><Button className="primary-action" size="sm" variant="primary" onClick={() => openReview({ kind: "report", item: report, action: "approve" })}><Check size={15} />通过并派单</Button></div>}
        </article>)}
      </div>
    </>}

    {tab === "tasks" && <>
      <div className="filter-row admin-filters"><button className={taskStatus === "all" ? "active" : ""} onClick={() => setTaskStatus("all")}>全部 {taskTotal}</button>{(["open", "claimed", "submitted", "verified", "cancelled"] as const).map((status) => <button key={status} className={taskStatus === status ? "active" : ""} onClick={() => setTaskStatus(status)}>{taskLabels[status]} {taskCounts[status]}</button>)}</div>
      <div className="review-list">
        {!loading && visibleTasks.length === 0 && <div className="panel admin-empty"><Route size={24} /><strong>当前没有{taskStatus === "all" ? "公共" : taskLabels[taskStatus]}任务</strong><span>上报审核通过并发布后会自动生成任务。</span></div>}
        {visibleTasks.map((task) => <article className="panel task-review-card" key={task.id}><div className="task-review-icon"><Route size={20} /></div><div className="review-copy"><div className="review-title"><Chip className={`task-state task-state-${task.status}`} size="sm" variant="soft">{taskLabels[task.status]}</Chip><h2>{task.title}</h2><small>{task.id}</small></div><p>{task.description}</p><div className="review-meta"><span><MapPinned size={14} />{task.address}</span><span>障碍编号：{task.obstacleId}</span>{task.assigneeId && <span>认领人：{task.assigneeName || task.assigneeId}{task.assigneeEmail ? `（${task.assigneeEmail}）` : ""}</span>}{task.completionNote && <span>处置说明：{task.completionNote}</span>}</div>{task.reviewNote && <div className="review-note">后台说明：{task.reviewNote}</div>}</div>
          {task.status === "submitted" && <div className="review-actions"><a className="secondary-action" href={`/api/v1/admin/tasks/${encodeURIComponent(task.id)}/evidence`} target="_blank" rel="noreferrer">查看凭证</a><Button className="secondary-action danger-action" size="sm" variant="ghost" onClick={() => openReview({ kind: "task", item: task, action: "return" })}><X size={15} />退回</Button><Button className="primary-action" size="sm" variant="primary" onClick={() => openReview({ kind: "task", item: task, action: "verify" })}><Check size={15} />确认闭环</Button></div>}
          {task.status === "cancelled" && <div className="review-actions"><Button className="primary-action" size="sm" variant="primary" onClick={() => openReview({ kind: "task", item: task, action: "reopen" })}><RefreshCw size={15} />重新发布</Button></div>}
        </article>)}
      </div>
    </>}

    <Modal isOpen={Boolean(reviewDialog)} onOpenChange={(open) => { if (!open) closeReview(); }}>
      <Modal.Backdrop variant="blur" isDismissable={!actionPending}>
        <Modal.Container size="md" placement="center">
          <Modal.Dialog className="review-dialog">
            <Modal.CloseTrigger aria-label="关闭审核窗口" isDisabled={actionPending} />
            <Modal.Header><Modal.Icon><ShieldAlert size={19} /></Modal.Icon><Modal.Heading>{reviewDialog ? actionLabels[reviewDialog.action] : "审核操作"}</Modal.Heading></Modal.Header>
            <Modal.Body>
              <p>{actionNeedsReason ? "请填写具体原因，内容将同步到上报人或任务处置人。" : "确认本次操作。可填写备注，便于后续追踪。"}</p>
              <label className="review-note-field"><span>{actionNeedsReason ? "处理原因" : "审核备注"}</span><TextArea value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} placeholder={actionNeedsReason ? "至少输入 2 个字符" : "选填"} rows={4} autoFocus /></label>
              {actionNeedsReason && reviewNote.trim().length > 0 && reviewNote.trim().length < 2 && <span className="review-note-error">原因至少需要 2 个字符</span>}
            </Modal.Body>
            <Modal.Footer><Button variant="ghost" onClick={closeReview} isDisabled={actionPending}>取消</Button><Button variant={reviewDialog?.action === "reject" || reviewDialog?.action === "return" || reviewDialog?.action === "cancel" ? "danger" : "primary"} onClick={() => void submitReview()} isDisabled={actionPending || Boolean(actionNeedsReason && reviewNote.trim().length < 2)}>{actionPending ? "提交中..." : reviewDialog ? actionLabels[reviewDialog.action] : "确认"}</Button></Modal.Footer>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
    </Modal>
  </div>;
}
