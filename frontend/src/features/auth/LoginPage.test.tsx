import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LoginPage } from "./LoginPage";

describe("LoginPage", () => {
  it("defaults the username to admin without defaulting the password", () => {
    render(<LoginPage onLogin={vi.fn()} />);

    expect(screen.getByLabelText("用户名")).toHaveValue("admin");
    expect(screen.getByLabelText("密码")).toHaveValue("");
  });

  it("shows one non-secret credential error", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn().mockRejectedValue(new Error("AUTH_INVALID: username=wrong-user password=wrong-password"));
    render(<LoginPage onLogin={onLogin} />);

    await user.clear(screen.getByLabelText("用户名"));
    await user.type(screen.getByLabelText("用户名"), "wrong-user");
    await user.type(screen.getByLabelText("密码"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("用户名或密码不正确");
    expect(screen.getByRole("alert")).not.toHaveTextContent("wrong-user");
    expect(screen.getByRole("alert")).not.toHaveTextContent("wrong-password");
  });
});
