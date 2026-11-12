"use client";

import {
  AlertTriangle,
  Camera,
  Check,
  LockKeyhole,
  Mail,
  Save,
  UserRound,
  X
} from "lucide-react";
import {
  type ChangeEvent,
  type FormEvent,
  useCallback,
  useRef,
  useState
} from "react";
import { api, type User } from "./api";
import { useApiResource } from "./hooks";
import { RequestState } from "./primitives";

export function AccountProfile({ showHeading = true }: { showHeading?: boolean } = {}) {
  const load = useCallback(() => api.me(), []);
  const { data: loadedUser, error, loading, reload } = useApiResource(load);
  const [user, setUser] = useState<User | null>(null);
  const currentUser = user ?? loadedUser;
  const fileInput = useRef<HTMLInputElement>(null);
  const [profileMessage, setProfileMessage] = useState<string | null>(null);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [avatarBusy, setAvatarBusy] = useState(false);
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [showEmailBinding, setShowEmailBinding] = useState(false);
  const [emailBusy, setEmailBusy] = useState(false);
  const [emailCodeBusy, setEmailCodeBusy] = useState(false);
  const [emailError, setEmailError] = useState<string | null>(null);
  const [emailMessage, setEmailMessage] = useState<string | null>(null);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  function applyUser(nextUser: User) {
    setUser(nextUser);
    window.dispatchEvent(new Event("wiki-user-updated"));
  }

  async function sendEmailBindingCode(form: HTMLFormElement) {
    const fields = new FormData(form);
    const email = String(fields.get("binding_email")).trim();
    setEmailError(null);
    setEmailMessage(null);
    setEmailCodeBusy(true);
    try {
      await api.sendEmailBindingCode(email);
      setEmailMessage("验证码已发送，请在 10 分钟内完成绑定。");
    } catch (cause) {
      setEmailError(cause instanceof Error ? cause.message : "验证码发送失败。");
    } finally {
      setEmailCodeBusy(false);
    }
  }

  async function bindEmail(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    setEmailError(null);
    setEmailMessage(null);
    setEmailBusy(true);
    try {
      applyUser(await api.bindEmail(
        String(fields.get("binding_email")).trim(),
        String(fields.get("email_code"))
      ));
      setShowEmailBinding(false);
      setProfileMessage("邮箱已绑定，之后不可更改。");
    } catch (cause) {
      setEmailError(cause instanceof Error ? cause.message : "邮箱绑定失败。");
    } finally {
      setEmailBusy(false);
    }
  }

  async function saveProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setProfileError(null);
    setProfileMessage(null);
    const fields = new FormData(event.currentTarget);
    try {
      applyUser(await api.updateProfile(String(fields.get("display_name"))));
      setProfileMessage("已保存");
    } catch (cause) {
      setProfileError(cause instanceof Error ? cause.message : "无法保存个人信息。");
    }
  }

  async function uploadAvatar(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setAvatarBusy(true);
    setProfileError(null);
    setProfileMessage(null);
    try {
      applyUser(await api.uploadAvatar(file));
      setProfileMessage("头像已更新");
    } catch (cause) {
      setProfileError(cause instanceof Error ? cause.message : "无法上传头像。");
    } finally {
      event.target.value = "";
      setAvatarBusy(false);
    }
  }

  async function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const nextPassword = String(fields.get("new_password"));
    setPasswordError(null);
    if (nextPassword !== String(fields.get("confirm_password"))) {
      setPasswordError("两次输入的新密码不一致。");
      return;
    }
    setPasswordBusy(true);
    try {
      await api.changePassword(String(fields.get("current_password")), nextPassword);
      window.location.assign("/login");
    } catch (cause) {
      setPasswordError(cause instanceof Error ? cause.message : "无法修改密码。");
      setPasswordBusy(false);
    }
  }

  async function deleteAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    if (String(fields.get("confirmation")) !== "注销账号") {
      setDeleteError("请输入“注销账号”进行确认。");
      return;
    }
    setDeleting(true);
    setDeleteError(null);
    try {
      await api.deleteAccount(String(fields.get("current_password")));
      window.location.assign("/login");
    } catch (cause) {
      setDeleteError(cause instanceof Error ? cause.message : "无法注销账号。");
      setDeleting(false);
    }
  }

  return (
    <div className="settings-page settings-page-narrow">
      {showHeading ? (
        <div className="settings-heading">
          <div className="settings-heading-icon"><UserRound size={22} /></div>
          <div>
            <span className="eyebrow">账号设置</span>
            <h1>个人中心</h1>
            <p>管理个人资料与登录安全。</p>
          </div>
        </div>
      ) : null}
      <RequestState error={error} loading={loading} onRetry={reload} />
      {currentUser ? (
        <>
          {currentUser.must_change_password || !currentUser.email ? (
            <div className="settings-card" role="status">
              <h2>账号安全建议</h2>
              <p>建议修改初始密码并绑定邮箱，方便保护账号和找回密码；这不是必做步骤，可随时继续使用其他功能。</p>
            </div>
          ) : null}
          <section className="settings-card profile-summary-card">
            <button
              aria-label="上传头像"
              className="profile-avatar-button"
              disabled={avatarBusy}
              onClick={() => fileInput.current?.click()}
              type="button"
            >
              <span className="profile-avatar-preview">
                {currentUser.avatar_url ? (
                  <img alt="当前头像" src={api.avatarUrl(currentUser.updated_at)} />
                ) : (
                  <UserRound size={32} />
                )}
              </span>
              <span className="profile-avatar-overlay">
                <Camera size={18} />
              </span>
            </button>
            <input
              accept="image/png,image/jpeg,image/webp"
              hidden
              onChange={(event) => void uploadAvatar(event)}
              ref={fileInput}
              type="file"
            />
            <div className="profile-summary-text">
              <strong>{currentUser.display_name}</strong>
              <span>{currentUser.email ?? "未绑定邮箱"}</span>
              <small>{avatarBusy ? "正在上传头像…" : "点击头像上传图片，最大 2 MB"}</small>
            </div>
          </section>

          <section className="settings-card">
            <div className="settings-card-head">
              <div>
                <h2>基本信息</h2>
                <p>账号用于登录；邮箱绑定后不可修改。</p>
              </div>
            </div>
            <form className="profile-detail-form" onSubmit={(event) => void saveProfile(event)}>
              <label className="field field-with-leading-icon">
                <span>显示名称</span>
                <UserRound size={16} />
                <input defaultValue={currentUser.display_name} maxLength={120} name="display_name" required />
              </label>
              <div className="verification-row">
                <label className="field field-with-leading-icon">
                  <span>邮箱</span>
                  <Mail size={16} />
                  <input disabled placeholder="尚未绑定" value={currentUser.email ?? ""} />
                </label>
                {!currentUser.email ? (
                  <button
                    className="button button-secondary code-button"
                    onClick={() => setShowEmailBinding((showing) => !showing)}
                    type="button"
                  >
                    <Mail size={14} /> {showEmailBinding ? "取消绑定" : "绑定邮箱"}
                  </button>
                ) : null}
              </div>
              {profileError ? <div className="settings-inline-error" role="alert">{profileError}</div> : null}
              <div className="profile-form-footer">
                <span className="profile-save-state">
                  {profileMessage ? <><Check size={13} /> {profileMessage}</> : null}
                </span>
                <button className="button" type="submit"><Save size={14} /> 保存</button>
              </div>
            </form>
            {!currentUser.email && showEmailBinding ? (
              <form className="profile-detail-form" onSubmit={(event) => void bindEmail(event)}>
                <p>绑定后可用于找回密码，绑定成功后不可更改。</p>
                <label className="field field-with-leading-icon">
                  <span>待绑定邮箱</span>
                  <Mail size={16} />
                  <input autoComplete="email" name="binding_email" required type="email" />
                </label>
                <div className="verification-row">
                  <label className="field">
                    <span>邮箱验证码</span>
                    <input
                      autoComplete="one-time-code"
                      inputMode="numeric"
                      maxLength={6}
                      name="email_code"
                      pattern="[0-9]{6}"
                      placeholder="6 位数字"
                      required
                    />
                  </label>
                  <button
                    className="button button-secondary code-button"
                    disabled={emailCodeBusy}
                    onClick={(event) => {
                      const form = event.currentTarget.form;
                      if (form) void sendEmailBindingCode(form);
                    }}
                    type="button"
                  >
                    {emailCodeBusy ? "发送中…" : "发送验证码"}
                  </button>
                </div>
                {emailMessage ? <div className="form-notice" role="status">{emailMessage}</div> : null}
                {emailError ? <div className="settings-inline-error" role="alert">{emailError}</div> : null}
                <div className="profile-form-footer">
                  <span />
                  <button className="button" disabled={emailBusy} type="submit">
                    {emailBusy ? "正在绑定…" : "验证并绑定"}
                  </button>
                </div>
              </form>
            ) : null}
          </section>

          <section className="settings-card">
            <div className="settings-card-head">
              <div>
                <h2>修改密码</h2>
                <p>密码更新后，所有设备都需要重新登录。</p>
              </div>
            </div>
            <form className="profile-detail-form" onSubmit={(event) => void changePassword(event)}>
              <label className="field field-with-leading-icon">
                <span>当前密码</span>
                <LockKeyhole size={16} />
                <input autoComplete="current-password" name="current_password" placeholder="输入当前密码" required type="password" />
              </label>
              <div className="settings-form-row">
                <label className="field field-with-leading-icon">
                  <span>新密码</span>
                  <LockKeyhole size={16} />
                  <input autoComplete="new-password" minLength={12} name="new_password" placeholder="至少 12 位" required type="password" />
                </label>
                <label className="field field-with-leading-icon">
                  <span>确认新密码</span>
                  <LockKeyhole size={16} />
                  <input autoComplete="new-password" minLength={12} name="confirm_password" placeholder="再次输入新密码" required type="password" />
                </label>
              </div>
              {passwordError ? <div className="settings-inline-error" role="alert">{passwordError}</div> : null}
              <div className="profile-form-footer">
                <span />
                <button className="button" disabled={passwordBusy} type="submit">
                  {passwordBusy ? "正在修改…" : "修改密码"}
                </button>
              </div>
            </form>
          </section>

          <section className="settings-card danger-zone">
            <div>
              <h2><AlertTriangle size={16} /> 危险操作</h2>
              <p>注销后无法恢复，个人模型配置会被永久删除。</p>
            </div>
            <button className="button button-danger-outline" onClick={() => setShowDeleteDialog(true)} type="button">注销账号</button>
          </section>
        </>
      ) : null}

      {showDeleteDialog ? (
        <div className="settings-modal-backdrop" role="presentation" onMouseDown={() => setShowDeleteDialog(false)}>
          <div aria-labelledby="delete-account-title" aria-modal="true" className="settings-modal settings-modal-small" onMouseDown={(event) => event.stopPropagation()} role="dialog">
            <div className="settings-modal-head">
              <div>
                <span className="eyebrow danger-text">危险操作</span>
                <h2 id="delete-account-title">确认注销账号</h2>
              </div>
              <button aria-label="关闭" onClick={() => setShowDeleteDialog(false)} type="button"><X size={18} /></button>
            </div>
            <div className="danger-notice"><AlertTriangle size={18} /><span>账号和个人模型凭据将被删除，此操作无法撤销。</span></div>
            <form className="settings-form" onSubmit={(event) => void deleteAccount(event)}>
              <label className="field">当前密码<input autoComplete="current-password" name="current_password" required type="password" /></label>
              <label className="field">输入“注销账号”确认<input autoComplete="off" name="confirmation" required /></label>
              {deleteError ? <div className="settings-inline-error" role="alert">{deleteError}</div> : null}
              <div className="settings-modal-actions">
                <button className="button button-secondary" onClick={() => setShowDeleteDialog(false)} type="button">取消</button>
                <button className="button button-danger" disabled={deleting} type="submit">{deleting ? "正在注销…" : "确认注销"}</button>
              </div>
            </form>
          </div>
        </div>
      ) : null}
    </div>
  );
}
