"use client";

import { flexRender, getCoreRowModel, useReactTable, type ColumnDef } from "@tanstack/react-table";
import { useEffect, useMemo, useState } from "react";
import { z } from "zod";
import { api, can } from "@/lib/api-client";
import { type Me, type SourceFile, fileSchema } from "@/lib/schemas";

export function Sources({ me, onMap }: { me: Me; onMap: (fileId: string) => void }) {
  const [files, setFiles] = useState<SourceFile[]>([]);
  const [message, setMessage] = useState("");
  const refresh = () => api("/files", z.array(fileSchema)).then(setFiles).catch((error: unknown) => setMessage(error instanceof Error ? error.message : "Unable to load sources"));
  useEffect(() => { void refresh(); }, []);

  const columns = useMemo<ColumnDef<SourceFile>[]>(() => [
    { accessorKey: "filename", header: "File" },
    { accessorKey: "status", header: "Status", cell: ({ getValue }) => <span className="status">{String(getValue())}</span> },
    { accessorKey: "row_count", header: "Rows", cell: ({ getValue }) => String(getValue() ?? "—") },
    { accessorKey: "byte_size", header: "Size", cell: ({ getValue }) => `${String(getValue())} B` },
    { id: "action", header: "", cell: ({ row }) => <button onClick={() => onMap(row.original.id)}>Profile & map</button> },
  ], [onMap]);
  const table = useReactTable({ data: files, columns, getCoreRowModel: getCoreRowModel() });

  async function upload(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const input = event.currentTarget.elements.namedItem("source") as HTMLInputElement;
    if (!input.files?.[0]) return;
    const body = new FormData();
    body.set("file", input.files[0]);
    try {
      const created = await api("/files", fileSchema, { method: "POST", body });
      setMessage(`${created.filename} uploaded and profiled. Confirm its mapping before ingestion.`);
      input.value = "";
      await refresh();
      onMap(created.id);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Upload failed");
    }
  }

  return (
    <section>
      <div className="page-heading"><div><p className="eyebrow">Source control</p><h1>Source files</h1><p>Uploading never ingests automatically. Mapping confirmation is a separate control.</p></div></div>
      {can(me.permissions, "data:upload") && (
        <form className="upload card" onSubmit={(event) => void upload(event)}>
          <label htmlFor="source">CSV, Excel, or supported source file</label>
          <input id="source" name="source" type="file" required />
          <button className="primary" type="submit">Upload for profiling</button>
        </form>
      )}
      {message && <p className="notice" role="status">{message}</p>}
      <div className="table-wrap"><table>
        <thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th key={header.id}>{flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead>
        <tbody>{table.getRowModel().rows.map((row) => <tr key={row.id}>{row.getVisibleCells().map((cell) => <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody>
      </table></div>
    </section>
  );
}
