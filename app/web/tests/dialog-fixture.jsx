import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { Context } from "../src/ui";
import AccountCreateDialog from "../src/pages/AccountCreateDialog";
import KeyCreateDialog from "../src/pages/KeyCreateDialog";
import "../src/style.css";

function Fixture() {
  const [active, setActive] = useState("");
  const [refreshCount, setRefreshCount] = useState(0);
  return (
    <Context.Provider
      value={{
        data: { platforms: ["demo-platform"] },
        refresh: () => setRefreshCount((n) => n + 1),
      }}
    >
      <button onClick={() => setActive("account")}>打开账户创建</button>
      <button onClick={() => setActive("key")}>打开密钥创建</button>
      <output data-testid="refresh-count">{refreshCount}</output>
      {active === "account" && (
        <AccountCreateDialog onClose={() => setActive("")} />
      )}
      {active === "key" && <KeyCreateDialog onClose={() => setActive("")} />}
    </Context.Provider>
  );
}

createRoot(document.getElementById("root")).render(<Fixture />);
