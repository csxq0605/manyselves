import { type FormEvent, useState } from "react";

import { BrandMark } from "../../design/BrandMark";
import "./login.css";

export interface LoginPageProps {
  readonly onLogin: (input: { username: string; password: string }) => Promise<void> | void;
}

export function LoginPage({ onLogin }: LoginPageProps) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError(false);
    setSubmitting(true);
    try {
      await onLogin({ password, username });
    } catch {
      setError(true);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="login-page">
      <section aria-labelledby="login-title" className="login-card">
        <div className="brand-lockup login-brand" aria-hidden="true"><BrandMark size={28} /><span>Manyselves</span></div>
        <h1 id="login-title">登录 Manyselves</h1>
        <p className="login-summary">一个运行时，许多个自己。</p>
        <form onSubmit={(event) => void submit(event)}>
          <label htmlFor="login-username">用户名</label>
          <input
            autoComplete="username"
            id="login-username"
            onChange={(event) => setUsername(event.target.value)}
            required
            value={username}
          />
          <label htmlFor="login-password">密码</label>
          <input
            autoComplete="current-password"
            id="login-password"
            onChange={(event) => setPassword(event.target.value)}
            required
            type="password"
            value={password}
          />
          {error ? <p className="login-error" role="alert">用户名或密码不正确</p> : null}
          <button disabled={submitting} type="submit">{submitting ? "正在登录" : "登录"}</button>
        </form>
      </section>
    </main>
  );
}
