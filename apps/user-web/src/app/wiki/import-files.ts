const SUPPORTED_EXTENSIONS = new Set(["pdf", "docx", "md", "txt", "html", "htm"]);

export type ImportFile = { file: File; path: string };

export function prepareImportFiles(files: ImportFile[]): {
  supported: ImportFile[];
  skipped: string[];
} {
  const supported: ImportFile[] = [];
  const skipped: string[] = [];
  for (const item of files) {
    const extension = item.file.name.split(".").at(-1)?.toLowerCase();
    if (item.file.size === 0) {
      skipped.push(`${item.path}（空文件）`);
    } else if (!extension || !SUPPORTED_EXTENSIONS.has(extension)) {
      skipped.push(`${item.path}（不支持的格式）`);
    } else {
      supported.push(item);
    }
  }
  return { supported, skipped };
}

export function selectedFiles(files: FileList): ImportFile[] {
  return Array.from(files, (file) => ({
    file,
    path: file.webkitRelativePath || file.name
  }));
}

async function entryFiles(entry: FileSystemEntry): Promise<ImportFile[]> {
  if (entry.isFile) {
    const file = await new Promise<File>((resolve, reject) => {
      (entry as FileSystemFileEntry).file(resolve, reject);
    });
    return [{ file, path: entry.fullPath.replace(/^\//, "") }];
  }
  if (!entry.isDirectory) {
    throw new Error(`无法读取拖入的项目：${entry.name}`);
  }
  const reader = (entry as FileSystemDirectoryEntry).createReader();
  const files: ImportFile[] = [];
  while (true) {
    const entries = await new Promise<FileSystemEntry[]>((resolve, reject) => {
      reader.readEntries(resolve, reject);
    });
    if (!entries.length) break;
    for (const child of entries) files.push(...await entryFiles(child));
  }
  return files;
}

export async function droppedFiles(dataTransfer: DataTransfer): Promise<ImportFile[]> {
  const entries = Array.from(dataTransfer.items)
    .filter((item) => item.kind === "file")
    .map((item) => item.webkitGetAsEntry?.() ?? null);
  if (entries.length && entries.every((entry) => entry !== null)) {
    const files: ImportFile[] = [];
    for (const entry of entries) files.push(...await entryFiles(entry!));
    return files;
  }
  if (entries.some((entry) => entry !== null)) {
    throw new Error("无法读取拖入的部分文件夹，请使用“选择文件夹”重新导入。");
  }
  return selectedFiles(dataTransfer.files);
}
