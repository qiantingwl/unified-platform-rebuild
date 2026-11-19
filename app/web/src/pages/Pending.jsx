import React from "react";
import { Head, Button } from "../ui";
export default function Pending() {
  return (
    <>
      <Head name="页面不存在" desc="此地址没有对应的管理页面。" />
      <Button onClick={() => (location.hash = "overview")}>返回运行总览</Button>
    </>
  );
}
