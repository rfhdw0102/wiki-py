export function sourceDeletionErrorMessage(message: string, status?: number): string {
  if (status === 409) {
    if (message.includes("still being processed")) {
      return "资料仍在处理，完成后才能删除。";
    }
    if (message.includes("referenced by Wiki pages")) {
      return "此来源仍被 Wiki 页面引用，删除未完成。";
    }
    if (message.includes("referenced by a draft")) {
      return "此来源仍被草稿引用，删除未完成。";
    }
    if (message.includes("Wiki changed; retry source deletion")) {
      return "Wiki 内容已更新，请重试删除。";
    }
  }
  if (message.includes("object cleanup failed")) {
    return "来源已移除，但原始文件清理失败，请联系管理员检查。";
  }
  if (message.includes("Wiki cleanup completed")) {
    return "Wiki 内容已清理，但来源记录删除失败，请刷新后重试。";
  }
  if (message.includes("Source deletion failed")) {
    return "删除未完成，请刷新后重试。";
  }
  return message || "删除失败，请稍后重试。";
}
