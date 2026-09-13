import { useEffect } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { fetchAccountStatus } from "../lib/account";
import { useStore } from "../lib/store";
import { PageShell } from "../components/layout/PageShell";
import { PageHeader } from "../components/layout/PageHeader";
import { LoadingState } from "../components/states/States";
import { AccountLogin } from "../features/account/AccountLogin";

/** 登录页路由（/login）：已登录直接去个人页；登录成功回个人页。 */
export function LoginView() {
  const account = useStore((s) => s.account);
  const setAccount = useStore((s) => s.setAccount);
  const navigate = useNavigate();

  useEffect(() => {
    fetchAccountStatus()
      .then(setAccount)
      .catch(() => undefined);
  }, [setAccount]);

  if (account === null) {
    return (
      <PageShell header={<PageHeader title="登录" />}>
        <LoadingState />
      </PageShell>
    );
  }
  if (account.logged_in) {
    return <Navigate to="/me" replace />;
  }
  return (
    <PageShell header={<PageHeader title="登录" />} surface="subtle">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          minHeight: "100%",
        }}
      >
        <AccountLogin
          onSuccess={() => {
            fetchAccountStatus()
              .then(setAccount)
              .catch(() => undefined);
            navigate("/me");
          }}
        />
      </div>
    </PageShell>
  );
}
