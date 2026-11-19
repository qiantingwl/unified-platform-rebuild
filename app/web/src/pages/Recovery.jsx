import React from "react";
import JobHistory from "./JobHistory";
import RecoveryMaterials from "./RecoveryMaterials";
import { Head, Panel, Tabs, Notice } from "../ui";
export default function Recovery() {
  return (
    <>
      <Head
        name="重登与恢复"
        desc="按原账户身份恢复，保留每次登录与核验回执。"
      />
      <Tabs group="maintenance" />
      <Notice>
        重登每账户每天最多一次，只有明确未收到验证码连续三次才停止。模型权限恢复需在账户详情安排真实回答验证。
      </Notice>
      <RecoveryMaterials />
      <Panel name="恢复任务记录">
        <JobHistory kinds="relogin,verify,renew" />
      </Panel>
    </>
  );
}
