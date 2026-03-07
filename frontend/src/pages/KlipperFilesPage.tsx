import { useState, useRef, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  ArrowLeft,
  Upload,
  FileText,
  Play,
  HardDrive,
  Loader2,
  CheckCircle,
  XCircle,
  Printer,
  RefreshCw,
} from 'lucide-react';
import { api } from '../api/client';
import { useToast } from '../contexts/ToastContext';

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatModified(ts: number): string {
  return new Date(ts * 1000).toLocaleString();
}

export function KlipperFilesPage() {
  const { printerId: printerIdStr } = useParams<{ printerId: string }>();
  const printerId = Number(printerIdStr);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [activeTab, setActiveTab] = useState<'upload' | 'files'>('upload');

  // ── Upload state ────────────────────────────────────────────────────────────
  const [dragOver, setDragOver] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [startAfterUpload, setStartAfterUpload] = useState(true);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [uploadResult, setUploadResult] = useState<{ success: boolean; message: string } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ── Printer info ────────────────────────────────────────────────────────────
  const { data: printer } = useQuery({
    queryKey: ['printer', printerId],
    queryFn: () => api.getPrinter(printerId),
    retry: 1,
  });

  // ── Files list ──────────────────────────────────────────────────────────────
  const {
    data: files,
    isLoading: filesLoading,
    refetch: refetchFiles,
  } = useQuery({
    queryKey: ['klipperFiles', printerId],
    queryFn: () => api.listKlipperFiles(printerId),
    enabled: activeTab === 'files',
    staleTime: 10_000,
  });

  // ── Reprint mutation ────────────────────────────────────────────────────────
  const reprintMutation = useMutation({
    mutationFn: (filename: string) => api.reprintKlipperFile(printerId, filename),
    onSuccess: (result) => {
      if (result.success) {
        showToast('Print started', 'success');
      } else {
        showToast(result.message, 'error');
      }
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  // ── Drag & drop handlers ────────────────────────────────────────────────────
  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(true);
  }, []);

  const onDragLeave = useCallback(() => setDragOver(false), []);

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file && file.name.endsWith('.gcode')) {
      setSelectedFile(file);
      setUploadResult(null);
    } else {
      showToast('Only .gcode files are accepted', 'error');
    }
  }, [showToast]);

  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      setSelectedFile(file);
      setUploadResult(null);
    }
    e.target.value = '';
  };

  // ── Upload handler ──────────────────────────────────────────────────────────
  const handleUpload = async () => {
    if (!selectedFile) return;
    setUploadProgress(0);
    setUploadResult(null);

    try {
      const result = await api.uploadKlipperFile(
        printerId,
        selectedFile,
        startAfterUpload,
        (pct) => setUploadProgress(pct),
      );
      setUploadResult({ success: result.success, message: result.message });
      if (result.success) {
        showToast(result.message, 'success');
        setSelectedFile(null);
        // Invalidate file list so Files tab shows the new file
        queryClient.invalidateQueries({ queryKey: ['klipperFiles', printerId] });
      } else {
        showToast(result.message, 'error');
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Upload failed';
      setUploadResult({ success: false, message });
      showToast(message, 'error');
    } finally {
      setUploadProgress(null);
    }
  };

  const isUploading = uploadProgress !== null;

  return (
    <div className="min-h-screen bg-bambu-dark text-white">
      {/* Header */}
      <div className="border-b border-bambu-dark-tertiary bg-bambu-dark-secondary px-4 py-3 flex items-center gap-3">
        <button
          onClick={() => navigate(-1)}
          className="p-1.5 rounded hover:bg-bambu-dark-tertiary text-bambu-gray hover:text-white transition-colors"
        >
          <ArrowLeft className="w-5 h-5" />
        </button>
        <Printer className="w-5 h-5 text-bambu-green" />
        <div>
          <h1 className="text-sm font-semibold">
            {printer?.name ?? `Printer ${printerId}`}
          </h1>
          <p className="text-xs text-bambu-gray">Klipper File Manager</p>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-bambu-dark-tertiary px-4">
        <div className="flex gap-0">
          {(['upload', 'files'] as const).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors capitalize ${
                activeTab === tab
                  ? 'border-bambu-green text-white'
                  : 'border-transparent text-bambu-gray hover:text-white'
              }`}
            >
              {tab === 'upload' ? 'Upload' : 'Files on Printer'}
            </button>
          ))}
        </div>
      </div>

      <div className="p-4 max-w-2xl">

        {/* ── Upload Tab ── */}
        {activeTab === 'upload' && (
          <div className="space-y-4">
            {/* Drop Zone */}
            <div
              onDragOver={onDragOver}
              onDragLeave={onDragLeave}
              onDrop={onDrop}
              onClick={() => !selectedFile && fileInputRef.current?.click()}
              className={`border-2 border-dashed rounded-xl p-10 flex flex-col items-center justify-center gap-3 transition-colors cursor-pointer ${
                dragOver
                  ? 'border-bambu-green bg-bambu-green/10'
                  : selectedFile
                  ? 'border-bambu-green/50 bg-bambu-dark-secondary cursor-default'
                  : 'border-bambu-dark-tertiary hover:border-bambu-green/40 bg-bambu-dark-secondary'
              }`}
            >
              {selectedFile ? (
                <>
                  <FileText className="w-10 h-10 text-bambu-green" />
                  <p className="text-sm font-medium text-white">{selectedFile.name}</p>
                  <p className="text-xs text-bambu-gray">{formatBytes(selectedFile.size)}</p>
                  {!isUploading && (
                    <button
                      onClick={(e) => { e.stopPropagation(); setSelectedFile(null); setUploadResult(null); }}
                      className="text-xs text-bambu-gray hover:text-red-400 transition-colors mt-1"
                    >
                      Remove
                    </button>
                  )}
                </>
              ) : (
                <>
                  <Upload className="w-10 h-10 text-bambu-gray" />
                  <p className="text-sm text-bambu-gray">Drag & drop a <span className="text-white">.gcode</span> file here</p>
                  <p className="text-xs text-bambu-gray">or click to browse</p>
                </>
              )}
            </div>

            <input
              ref={fileInputRef}
              type="file"
              accept=".gcode"
              className="hidden"
              onChange={onFileChange}
            />

            {/* Options */}
            <label className="flex items-center gap-2.5 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={startAfterUpload}
                onChange={(e) => setStartAfterUpload(e.target.checked)}
                className="w-4 h-4 accent-bambu-green"
              />
              <span className="text-sm text-bambu-gray">Start printing after upload</span>
            </label>

            {/* Progress bar */}
            {isUploading && (
              <div className="space-y-1.5">
                <div className="flex justify-between text-xs text-bambu-gray">
                  <span>Uploading…</span>
                  <span>{uploadProgress}%</span>
                </div>
                <div className="h-2 bg-bambu-dark-tertiary rounded-full overflow-hidden">
                  <div
                    className="h-full bg-bambu-green transition-all duration-200 rounded-full"
                    style={{ width: `${uploadProgress}%` }}
                  />
                </div>
              </div>
            )}

            {/* Result */}
            {uploadResult && !isUploading && (
              <div className={`flex items-center gap-2 text-sm rounded-lg p-3 ${
                uploadResult.success
                  ? 'bg-bambu-green/10 text-bambu-green border border-bambu-green/30'
                  : 'bg-red-500/10 text-red-400 border border-red-500/30'
              }`}>
                {uploadResult.success
                  ? <CheckCircle className="w-4 h-4 flex-shrink-0" />
                  : <XCircle className="w-4 h-4 flex-shrink-0" />}
                {uploadResult.message}
              </div>
            )}

            {/* Upload button */}
            <button
              onClick={handleUpload}
              disabled={!selectedFile || isUploading}
              className="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg text-sm font-medium bg-bambu-green text-black hover:bg-bambu-green/90 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              {isUploading ? (
                <><Loader2 className="w-4 h-4 animate-spin" /> Uploading…</>
              ) : (
                <><Upload className="w-4 h-4" /> Upload{startAfterUpload ? ' & Print' : ''}</>
              )}
            </button>
          </div>
        )}

        {/* ── Files Tab ── */}
        {activeTab === 'files' && (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <p className="text-xs text-bambu-gray">Files stored on the printer</p>
              <button
                onClick={() => refetchFiles()}
                className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors"
              >
                <RefreshCw className="w-3 h-3" />
                Refresh
              </button>
            </div>

            {filesLoading ? (
              <div className="flex items-center justify-center py-12 text-bambu-gray">
                <Loader2 className="w-5 h-5 animate-spin mr-2" />
                Loading files…
              </div>
            ) : !files?.length ? (
              <div className="flex flex-col items-center justify-center py-12 text-bambu-gray gap-2">
                <HardDrive className="w-8 h-8 opacity-40" />
                <p className="text-sm">No files found on this printer</p>
              </div>
            ) : (
              <div className="space-y-2">
                {files.map((f) => (
                  <div
                    key={f.filename}
                    className="flex items-center gap-3 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg px-3 py-2.5"
                  >
                    <FileText className="w-4 h-4 text-bambu-gray flex-shrink-0" />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-white truncate">{f.filename}</p>
                      <p className="text-xs text-bambu-gray">
                        {formatBytes(f.size)} · {formatModified(f.modified)}
                      </p>
                    </div>
                    <button
                      onClick={() => reprintMutation.mutate(f.filename)}
                      disabled={reprintMutation.isPending && reprintMutation.variables === f.filename}
                      title="Start printing this file"
                      className="flex items-center gap-1.5 px-2.5 py-1.5 rounded text-xs font-medium bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30 disabled:opacity-50 transition-colors flex-shrink-0"
                    >
                      {reprintMutation.isPending && reprintMutation.variables === f.filename ? (
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      ) : (
                        <Play className="w-3.5 h-3.5" />
                      )}
                      Print
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
