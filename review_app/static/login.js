"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  let email = "";
  const status = (message, error = false) => {
    $("message").textContent = message;
    $("message").classList.toggle("error", error);
  };
  async function post(path, payload) {
    const response = await fetch(path, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload), cache: "no-store",
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "请求失败，请稍后再试");
    return data;
  }
  async function sendCode() {
    email = $("email").value.trim().toLowerCase();
    $("send").disabled = true;
    $("resend").disabled = true;
    status("正在发送验证码…");
    try {
      const result = await post("./api/auth/request-code", {email});
      $("code-form").hidden = false;
      $("code").focus();
      status(result.message);
    } catch (error) {
      status(error.message, true);
    } finally {
      $("send").disabled = false;
      $("resend").disabled = false;
    }
  }
  $("email-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sendCode();
  });
  $("resend").addEventListener("click", sendCode);
  $("code-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("verify").disabled = true;
    status("正在验证…");
    try {
      await post("./api/auth/verify-code", {email, code: $("code").value.trim()});
      location.replace(new URL("./", document.baseURI));
    } catch (error) {
      status(error.message, true);
      $("verify").disabled = false;
    }
  });
})();
