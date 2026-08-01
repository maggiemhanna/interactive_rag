import React, { useState, useEffect, useRef } from 'react';
import { 
  Database, 
  Upload, 
  FileText, 
  PlusCircle, 
  CheckCircle2, 
  AlertCircle, 
  RefreshCw, 
  Layers, 
  FolderPlus,
  X,
  FileCheck,
  Server,
  Search,
  Sparkles,
  FileCode2,
  ChevronDown,
  ChevronUp,
  Bot,
  Terminal,
  Code2,
  Copy,
  Check
} from 'lucide-react';

const API_BASE = 'http://localhost:8000';
const AGENT_API_BASE = 'http://localhost:8001';

function PrettyPrintEventStream({ events }) {
  const [viewMode, setViewMode] = useState('pretty'); // 'pretty' | 'json'
  const [copied, setCopied] = useState(false);

  const handleCopyJson = () => {
    navigator.clipboard.writeText(JSON.stringify(events, null, 2));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="stream-inspector-container">
      <div className="stream-inspector-header">
        <div className="stream-inspector-title">
          <Terminal size={16} color="var(--primary)" />
          Stream Events Log ({events.length} message events)
        </div>
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          <button
            type="button"
            className={`tab-btn ${viewMode === 'pretty' ? 'active' : ''}`}
            onClick={() => setViewMode('pretty')}
            style={{ padding: '0.3rem 0.75rem', fontSize: '0.78rem' }}
          >
            <Sparkles size={13} />
            pretty_print_event
          </button>
          <button
            type="button"
            className={`tab-btn ${viewMode === 'json' ? 'active' : ''}`}
            onClick={() => setViewMode('json')}
            style={{ padding: '0.3rem 0.75rem', fontSize: '0.78rem' }}
          >
            <Code2 size={13} />
            Raw JSON
          </button>
          {viewMode === 'json' && (
            <button
              type="button"
              className="refresh-btn"
              onClick={handleCopyJson}
              title="Copy JSON"
              style={{ color: copied ? 'var(--accent-emerald)' : 'var(--text-muted)' }}
            >
              {copied ? <Check size={14} /> : <Copy size={14} />}
            </button>
          )}
        </div>
      </div>

      <div className="stream-inspector-body">
        {viewMode === 'json' ? (
          <pre style={{ margin: 0, color: '#a5b4fc', fontSize: '0.8rem' }}>
            {JSON.stringify(events, null, 2)}
          </pre>
        ) : (
          <div>
            {events.map((msg, index) => {
              const msgType = (msg.type || 'unknown').toLowerCase();

              if (msgType === 'human' || msgType === 'user') {
                return (
                  <div key={index}>
                    <div className="pretty-header human">
                      Human Message
                    </div>
                    <div className="pretty-msg-box">
                      {msg.content}
                    </div>
                  </div>
                );
              }

              if (msgType === 'ai' || msgType === 'assistant') {
                return (
                  <div key={index}>
                    <div className="pretty-header ai">
                      AI Message
                    </div>
                    {msg.content && (
                      <div className="pretty-msg-box">
                        {msg.content}
                      </div>
                    )}

                    {/* Tool Calls */}
                    {msg.tool_calls && msg.tool_calls.length > 0 && (
                      <div className="pretty-tool-calls-panel">
                        <div className="pretty-tool-calls-title">
                          <Bot size={15} />
                          Tool Calls
                        </div>
                        {msg.tool_calls.map((call, cIdx) => (
                          <div key={cIdx} style={{ marginBottom: cIdx < msg.tool_calls.length - 1 ? '0.5rem' : 0 }}>
                            <div style={{ fontWeight: 700, color: '#f43f5e', marginBottom: '4px' }}>
                              Tool: {call.name}
                            </div>
                            {call.args && (
                              <div>
                                <div style={{ fontWeight: 600, color: '#94a3b8', fontSize: '0.8rem' }}>
                                  Arguments:
                                </div>
                                {Object.entries(call.args).map(([k, v]) => (
                                  <div key={k} className="pretty-tool-arg-row">
                                    <span className="pretty-tool-arg-key">  • {k}:</span>
                                    <span>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              }

              if (msgType === 'tool' || msgType === 'function') {
                return (
                  <div key={index}>
                    <div className="pretty-header tool">
                      Tool Message
                    </div>
                    <div style={{ fontSize: '0.82rem', marginBottom: '4px', fontWeight: 600, color: '#f87171' }}>
                      Tool Name: {msg.name || 'Retriever'}
                    </div>
                    <div style={{ fontSize: '0.8rem', fontWeight: 600, color: '#e2e8f0', marginBottom: '4px' }}>
                      Tool Result:
                    </div>
                    <div className="pretty-tool-result-box">
                      {msg.content}
                    </div>
                  </div>
                );
              }

              if (msgType === 'system') {
                return (
                  <div key={index}>
                    <div className="pretty-header system">
                      System Message
                    </div>
                    <div className="pretty-msg-box">
                      {msg.content}
                    </div>
                  </div>
                );
              }

              return (
                <div key={index}>
                  <div className="pretty-header" style={{ color: '#94a3b8' }}>
                    {msgType.toUpperCase()} Message
                  </div>
                  <div className="pretty-msg-box">
                    {msg.content || JSON.stringify(msg)}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

export default function App() {
  const [activeTab, setActiveTab] = useState('ingest'); // 'ingest' | 'query' | 'agent'

  const [collections, setCollections] = useState([]);
  const [collectionMode, setCollectionMode] = useState('existing'); // 'existing' | 'new'
  const [selectedCollection, setSelectedCollection] = useState('');
  const [newCollectionName, setNewCollectionName] = useState('');
  
  const [uploadMode, setUploadMode] = useState('files'); // 'files' | 'paths'
  const [files, setFiles] = useState([]);
  const [pdfPaths, setPdfPaths] = useState('');
  
  const [loading, setLoading] = useState(false);
  const [apiStatus, setApiStatus] = useState('checking');
  const [notification, setNotification] = useState(null);
  
  // Query tab state
  const [queryCollection, setQueryCollection] = useState('');
  const [queryText, setQueryText] = useState('');
  const [topK, setTopK] = useState(5);
  const [queryLoading, setQueryLoading] = useState(false);
  const [queryResults, setQueryResults] = useState(null);
  const [expandedMeta, setExpandedMeta] = useState({});

  // Agent tab state
  const [agentCollection, setAgentCollection] = useState('');
  const [agentQuery, setAgentQuery] = useState('');
  const [agentLoading, setAgentLoading] = useState(false);
  const [agentAnswer, setAgentAnswer] = useState(null);
  const [agentEvents, setAgentEvents] = useState([]);
  const [showStreamEvents, setShowStreamEvents] = useState(false);

  const fileInputRef = useRef(null);

  // Fetch collections from backend
  const fetchCollections = async () => {
    try {
      setApiStatus('checking');
      let res;
      try {
        res = await fetch(`${API_BASE}/collections`);
      } catch (e) {
        res = await fetch(`${AGENT_API_BASE}/collections`);
      }
      if (!res.ok) throw new Error('Failed to fetch collections');
      const data = await res.json();
      const colList = data.collections || [];
      setCollections(colList);
      setApiStatus('online');
      
      if (colList.length > 0) {
        if (!selectedCollection) setSelectedCollection(colList[0]);
        if (!queryCollection) setQueryCollection(colList[0]);
        if (!agentCollection) setAgentCollection(colList[0]);
      }
    } catch (err) {
      console.error('Error connecting to backend:', err);
      setApiStatus('offline');
      setNotification({
        type: 'error',
        message: 'Could not connect to FastAPI server on http://localhost:8000 or http://localhost:8001. Please ensure backend is running.'
      });
    }
  };

  useEffect(() => {
    fetchCollections();
  }, []);

  const handleFileChange = (e) => {
    const selectedFiles = Array.from(e.target.files).filter(
      (f) => f.type === 'application/pdf' || f.name.toLowerCase().endsWith('.pdf')
    );
    if (selectedFiles.length === 0) {
      setNotification({
        type: 'error',
        message: 'Please select valid PDF file(s).'
      });
      return;
    }
    setFiles((prev) => [...prev, ...selectedFiles]);
    setNotification(null);
  };

  const removeFile = (index) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const handleDrop = (e) => {
    e.preventDefault();
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      const droppedFiles = Array.from(e.dataTransfer.files).filter(
        (f) => f.type === 'application/pdf' || f.name.toLowerCase().endsWith('.pdf')
      );
      if (droppedFiles.length > 0) {
        setFiles((prev) => [...prev, ...droppedFiles]);
        setNotification(null);
      } else {
        setNotification({
          type: 'error',
          message: 'Only PDF files are supported.'
        });
      }
    }
  };

  const handleIngestSubmit = async (e) => {
    e.preventDefault();
    setNotification(null);

    const targetCollection = collectionMode === 'existing' ? selectedCollection : newCollectionName.trim();
    if (!targetCollection) {
      setNotification({
        type: 'error',
        message: 'Please select or enter a valid collection name.'
      });
      return;
    }

    if (uploadMode === 'files' && files.length === 0) {
      setNotification({
        type: 'error',
        message: 'Please choose at least one PDF file to upload.'
      });
      return;
    }

    if (uploadMode === 'paths' && !pdfPaths.trim()) {
      setNotification({
        type: 'error',
        message: 'Please enter at least one valid server PDF path.'
      });
      return;
    }

    setLoading(true);

    try {
      const formData = new FormData();
      formData.append('collection_name', targetCollection);

      if (uploadMode === 'files') {
        files.forEach((file) => {
          formData.append('files', file);
        });
      } else {
        formData.append('pdf_paths', pdfPaths.trim());
      }

      const response = await fetch(`${API_BASE}/add_documents`, {
        method: 'POST',
        body: formData,
      });

      const result = await response.json();

      if (!response.ok) {
        throw new Error(result.detail || 'Failed to ingest documents');
      }

      setNotification({
        type: 'success',
        message: result.message || `Successfully ingested documents into '${targetCollection}'!`,
        details: result
      });

      setFiles([]);
      setPdfPaths('');
      if (collectionMode === 'new') {
        setSelectedCollection(targetCollection);
        setCollectionMode('existing');
        setNewCollectionName('');
      }

      await fetchCollections();

    } catch (err) {
      console.error('Ingestion error:', err);
      setNotification({
        type: 'error',
        message: err.message || 'An error occurred while uploading documents.'
      });
    } finally {
      setLoading(false);
    }
  };

  const handleQuerySubmit = async (e) => {
    e.preventDefault();
    setNotification(null);
    setQueryResults(null);

    if (!queryCollection) {
      setNotification({
        type: 'error',
        message: 'Please select a collection to query.'
      });
      return;
    }

    if (!queryText.trim()) {
      setNotification({
        type: 'error',
        message: 'Please enter a search query.'
      });
      return;
    }

    setQueryLoading(true);

    try {
      const response = await fetch(`${API_BASE}/query`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          query: queryText.trim(),
          collection_name: queryCollection,
          k: Number(topK)
        }),
      });

      const result = await response.json();

      if (!response.ok) {
        throw new Error(result.detail || 'Failed to query collection');
      }

      setQueryResults(result.results || []);

    } catch (err) {
      console.error('Query error:', err);
      setNotification({
        type: 'error',
        message: err.message || 'An error occurred while querying vector store.'
      });
    } finally {
      setQueryLoading(false);
    }
  };

  const handleAgentSubmit = async (e) => {
    e.preventDefault();
    setNotification(null);
    setAgentAnswer(null);
    setAgentEvents([]);
    setShowStreamEvents(false);

    const targetCollection = agentCollection || selectedCollection;
    if (!targetCollection) {
      setNotification({
        type: 'error',
        message: 'Please select a collection for the Agentic RAG assistant.'
      });
      return;
    }

    if (!agentQuery.trim()) {
      setNotification({
        type: 'error',
        message: 'Please enter a question for the agent.'
      });
      return;
    }

    setAgentLoading(true);

    try {
      let agentUrl = `${AGENT_API_BASE}/agentic_rag`;
      let response;
      try {
        response = await fetch(agentUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            query: agentQuery.trim(),
            collection_name: targetCollection,
            stream: true,
          }),
        });
      } catch (err) {
        agentUrl = `${API_BASE}/agentic_rag`;
        response = await fetch(agentUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            query: agentQuery.trim(),
            collection_name: targetCollection,
            stream: true,
          }),
        });
      }

      if (!response.ok) {
        const errJson = await response.json().catch(() => ({}));
        throw new Error(errJson.detail || `Server error: ${response.status}`);
      }

      const contentType = response.headers.get('content-type') || '';
      if (contentType.includes('text/event-stream')) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        const accumulatedEvents = [];

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            const trimmed = line.trim();
            if (trimmed.startsWith('data: ')) {
              const dataStr = trimmed.slice(6).trim();
              if (dataStr === '[DONE]') break;

              try {
                const parsed = JSON.parse(dataStr);
                if (parsed.message) {
                  accumulatedEvents.push(parsed.message);
                  setAgentEvents([...accumulatedEvents]);

                  const msgType = (parsed.message.type || '').toLowerCase();
                  if ((msgType === 'ai' || msgType === 'assistant') && parsed.message.content) {
                    setAgentAnswer(parsed.message.content);
                  }
                }
              } catch (e) {
                console.warn('Error parsing SSE json:', e);
              }
            }
          }
        }
      } else {
        const data = await response.json();
        if (data.messages) {
          setAgentEvents(data.messages);
        }
        if (data.answer) {
          setAgentAnswer(data.answer);
        }
      }
    } catch (err) {
      console.error('Agent execution error:', err);
      setNotification({
        type: 'error',
        message: err.message || 'An error occurred while communicating with the AI agent.'
      });
    } finally {
      setAgentLoading(false);
    }
  };

  const toggleMetadata = (index) => {
    setExpandedMeta((prev) => ({
      ...prev,
      [index]: !prev[index]
    }));
  };

  return (
    <div className="app-container">
      {/* Header */}
      <header className="app-header">
        <div className="brand">
          <div className="logo-icon">
            <Database size={28} />
          </div>
          <div>
            <h1 className="brand-title">Interactive RAG Portal</h1>
            <p className="brand-subtitle">Vector Store Ingestion & Semantic Retrieval Engine</p>
          </div>
        </div>

        <div className="status-pill">
          <span className={`status-dot ${apiStatus === 'online' ? 'online' : 'offline'}`}></span>
          <span>{apiStatus === 'online' ? 'Backend Connected' : 'Backend Offline'}</span>
          <button onClick={fetchCollections} className="refresh-btn" title="Check connection">
            <RefreshCw size={14} className={apiStatus === 'checking' ? 'spinner' : ''} />
          </button>
        </div>
      </header>

      {/* Main Navigation Tabs */}
      <div className="nav-tabs">
        <button
          className={`nav-tab-btn ${activeTab === 'ingest' ? 'active' : ''}`}
          onClick={() => setActiveTab('ingest')}
        >
          <Upload size={18} />
          Ingest Documents
        </button>
        <button
          className={`nav-tab-btn ${activeTab === 'query' ? 'active' : ''}`}
          onClick={() => setActiveTab('query')}
        >
          <Search size={18} />
          Query Collections
        </button>
        <button
          className={`nav-tab-btn ${activeTab === 'agent' ? 'active' : ''}`}
          onClick={() => setActiveTab('agent')}
        >
          <Bot size={18} />
          Agentic RAG Assistant
        </button>
      </div>

      {/* Notification Banner */}
      {notification && (
        <div className={`alert ${notification.type === 'success' ? 'alert-success' : 'alert-error'}`}>
          {notification.type === 'success' ? (
            <CheckCircle2 size={20} style={{ flexShrink: 0 }} />
          ) : (
            <AlertCircle size={20} style={{ flexShrink: 0 }} />
          )}
          <div style={{ flex: 1 }}>
            <p style={{ fontWeight: 600 }}>{notification.message}</p>
            {notification.details && (
              <p style={{ fontSize: '0.8rem', marginTop: '4px', opacity: 0.9 }}>
                Added {notification.details.chunks_added} chunks across {notification.details.processed_files?.length || 0} file(s).
              </p>
            )}
          </div>
          <button 
            onClick={() => setNotification(null)}
            style={{ background: 'transparent', border: 'none', color: 'inherit', cursor: 'pointer' }}
          >
            <X size={16} />
          </button>
        </div>
      )}

      {/* Main Grid */}
      <div className="main-grid">
        {/* Left Section - Primary Action */}
        {activeTab === 'ingest' && (
          <div className="glass-card">
            <h2 className="card-title">
              <Upload size={22} color="var(--primary)" />
              Ingest PDF Documents
            </h2>

            <form onSubmit={handleIngestSubmit}>
              {/* Step 1: Collection Selection */}
              <div className="form-group">
                <label className="form-label">Vectorstore Collection Name</label>
                
                <div className="tab-group">
                  <button
                    type="button"
                    className={`tab-btn ${collectionMode === 'existing' ? 'active' : ''}`}
                    onClick={() => setCollectionMode('existing')}
                  >
                    <Layers size={16} />
                    Existing Collection
                  </button>
                  <button
                    type="button"
                    className={`tab-btn ${collectionMode === 'new' ? 'active' : ''}`}
                    onClick={() => setCollectionMode('new')}
                  >
                    <FolderPlus size={16} />
                    New Collection
                  </button>
                </div>

                {collectionMode === 'existing' ? (
                  <div>
                    {collections.length > 0 ? (
                      <select
                        className="form-select"
                        value={selectedCollection}
                        onChange={(e) => setSelectedCollection(e.target.value)}
                      >
                        {collections.map((col) => (
                          <option key={col} value={col}>
                            {col}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                        No existing collections found. Select 'New Collection' to create one.
                      </p>
                    )}
                  </div>
                ) : (
                  <input
                    type="text"
                    className="form-input"
                    placeholder="e.g. stock_market_vs, financial_reports_2025"
                    value={newCollectionName}
                    onChange={(e) => setNewCollectionName(e.target.value)}
                  />
                )}
              </div>

              {/* Step 2: Document Input Mode */}
              <div className="form-group">
                <label className="form-label">Document Input Mode</label>
                
                <div className="tab-group">
                  <button
                    type="button"
                    className={`tab-btn ${uploadMode === 'files' ? 'active' : ''}`}
                    onClick={() => setUploadMode('files')}
                  >
                    <Upload size={16} />
                    Upload PDF Files
                  </button>
                  <button
                    type="button"
                    className={`tab-btn ${uploadMode === 'paths' ? 'active' : ''}`}
                    onClick={() => setUploadMode('paths')}
                  >
                    <FileText size={16} />
                    Server File Paths
                  </button>
                </div>

                {uploadMode === 'files' ? (
                  <div>
                    <div
                      className="dropzone"
                      onDragOver={(e) => e.preventDefault()}
                      onDrop={handleDrop}
                      onClick={() => fileInputRef.current?.click()}
                    >
                      <Upload className="dropzone-icon" />
                      <p className="dropzone-text">Click or Drag & Drop PDF files here</p>
                      <p className="dropzone-hint">Supports single or multiple PDF documents</p>
                      <input
                        ref={fileInputRef}
                        type="file"
                        multiple
                        accept=".pdf,application/pdf"
                        onChange={handleFileChange}
                        style={{ display: 'none' }}
                      />
                    </div>

                    {files.length > 0 && (
                      <div className="file-list">
                        {files.map((file, idx) => (
                          <div key={idx} className="file-item">
                            <div className="file-info">
                              <FileCheck size={18} color="var(--accent-cyan)" />
                              <div>
                                <div className="file-name">{file.name}</div>
                                <div className="file-size">{(file.size / 1024).toFixed(1)} KB</div>
                              </div>
                            </div>
                            <button
                              type="button"
                              className="remove-file-btn"
                              onClick={() => removeFile(idx)}
                              title="Remove file"
                            >
                              <X size={16} />
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ) : (
                  <div>
                    <textarea
                      rows={4}
                      className="form-textarea"
                      placeholder="Enter file path(s), one per line, e.g.:&#10;./data/Stock_Market_Performance_2024.pdf&#10;./data/Stock_Market_Performance_2025.pdf"
                      value={pdfPaths}
                      onChange={(e) => setPdfPaths(e.target.value)}
                    />
                    <p style={{ fontSize: '0.75rem', color: 'var(--text-dim)', marginTop: '4px' }}>
                      Enter absolute or relative paths accessible by the FastAPI server.
                    </p>
                  </div>
                )}
              </div>

              {/* Step 3: Submit Button */}
              <button type="submit" className="btn-primary" disabled={loading || apiStatus === 'offline'}>
                {loading ? (
                  <>
                    <RefreshCw className="spinner" size={20} />
                    Splitting & Embedding PDF Chunks...
                  </>
                ) : (
                  <>
                    <PlusCircle size={20} />
                    Ingest Documents into Collection
                  </>
                )}
              </button>
            </form>
          </div>
        )}

        {/* Query Tab Content */}
        {activeTab === 'query' && (
          <div className="glass-card">
            <h2 className="card-title">
              <Search size={22} color="var(--accent-cyan)" />
              Search & Query Collection
            </h2>

            <form onSubmit={handleQuerySubmit}>
              {/* Target Collection */}
              <div className="form-group">
                <label className="form-label">Select Target Collection</label>
                {collections.length > 0 ? (
                  <select
                    className="form-select"
                    value={queryCollection}
                    onChange={(e) => setQueryCollection(e.target.value)}
                  >
                    {collections.map((col) => (
                      <option key={col} value={col}>
                        {col}
                      </option>
                    ))}
                  </select>
                ) : (
                  <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                    No collections found. Ingest documents first to create a collection.
                  </p>
                )}
              </div>

              {/* Query Text */}
              <div className="form-group">
                <label className="form-label">Natural Language Query / Question</label>
                <input
                  type="text"
                  className="form-input"
                  placeholder="e.g., What is Arm Holdings (ARM)? or Stock market trends in 2025"
                  value={queryText}
                  onChange={(e) => setQueryText(e.target.value)}
                />
              </div>

              {/* Top K selector */}
              <div className="form-group">
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
                  <label className="form-label" style={{ margin: 0 }}>Top Results Limit (k)</label>
                  <span style={{ fontSize: '0.85rem', fontWeight: 700, color: 'var(--accent-cyan)' }}>{topK} results</span>
                </div>
                <input
                  type="range"
                  min="1"
                  max="10"
                  value={topK}
                  onChange={(e) => setTopK(e.target.value)}
                  style={{ width: '100%', accentColor: 'var(--primary)', cursor: 'pointer' }}
                />
              </div>

              <button type="submit" className="btn-primary" disabled={queryLoading || apiStatus === 'offline'}>
                {queryLoading ? (
                  <>
                    <RefreshCw className="spinner" size={20} />
                    Retrieving Matching Documents...
                  </>
                ) : (
                  <>
                    <Sparkles size={20} />
                    Run Similarity Search
                  </>
                )}
              </button>
            </form>

            {/* Results Section */}
            {queryResults && (
              <div style={{ marginTop: '2rem' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', paddingBottom: '0.5rem', borderBottom: '1px solid var(--border-color)' }}>
                  <h3 style={{ fontSize: '1rem', fontWeight: 600, color: '#fff' }}>
                    Search Results ({queryResults.length})
                  </h3>
                  <span style={{ fontSize: '0.8rem', color: 'var(--text-dim)' }}>
                    Collection: <strong>{queryCollection}</strong>
                  </span>
                </div>

                {queryResults.length === 0 ? (
                  <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', margin: '1.5rem 0' }}>
                    No matching documents found for your query.
                  </p>
                ) : (
                  <div className="results-container">
                    {queryResults.map((res, idx) => (
                      <div key={idx} className="result-card">
                        <div className="result-header">
                          <span className="result-rank-badge">
                            Result #{idx + 1}
                          </span>

                          {res.metadata?.source && (
                            <span className="result-source-tag">
                              <FileCode2 size={13} />
                              {res.metadata.source.split('/').pop()}
                              {res.metadata.page_label !== undefined && ` (p. ${res.metadata.page_label})`}
                            </span>
                          )}
                        </div>

                        <div className="result-content">
                          {res.page_content}
                        </div>

                        {res.metadata && (
                          <div>
                            <button
                              type="button"
                              className="result-metadata-toggle"
                              onClick={() => toggleMetadata(idx)}
                            >
                              {expandedMeta[idx] ? (
                                <>
                                  <ChevronUp size={14} /> Hide Full Metadata
                                </>
                              ) : (
                                <>
                                  <ChevronDown size={14} /> View Metadata
                                </>
                              )}
                            </button>

                            {expandedMeta[idx] && (
                              <pre className="result-metadata-box">
                                {JSON.stringify(res.metadata, null, 2)}
                              </pre>
                            )}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Agentic RAG Assistant Tab Content */}
        {activeTab === 'agent' && (
          <div className="glass-card">
            <h2 className="card-title">
              <Bot size={22} color="var(--primary)" />
              Agentic RAG Assistant
            </h2>
            <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginBottom: '1.5rem' }}>
              Ask natural language questions to any collection. The agent dynamically decides when to invoke document retriever tools and returns a synthesized response.
            </p>

            <form onSubmit={handleAgentSubmit}>
              {/* Target Collection */}
              <div className="form-group">
                <label className="form-label">Select Target Collection</label>
                {collections.length > 0 ? (
                  <select
                    className="form-select"
                    value={agentCollection || selectedCollection}
                    onChange={(e) => setAgentCollection(e.target.value)}
                  >
                    {collections.map((col) => (
                      <option key={col} value={col}>
                        {col}
                      </option>
                    ))}
                  </select>
                ) : (
                  <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                    No collections found. Ingest documents first to create a collection.
                  </p>
                )}
              </div>

              {/* Natural Language Question */}
              <div className="form-group">
                <label className="form-label">Question for AI Agent</label>
                <textarea
                  rows={3}
                  className="form-textarea"
                  placeholder="e.g. What is ARM Holdings? or Summarize the major drivers for ARM in 2024."
                  value={agentQuery}
                  onChange={(e) => setAgentQuery(e.target.value)}
                />
              </div>

              <button type="submit" className="btn-primary" disabled={agentLoading || apiStatus === 'offline'}>
                {agentLoading ? (
                  <>
                    <RefreshCw className="spinner" size={20} />
                    Agent Thinking & Querying Retriever...
                  </>
                ) : (
                  <>
                    <Sparkles size={20} />
                    Ask Agent
                  </>
                )}
              </button>
            </form>

            {/* AI Response Card & Event Stream Inspector */}
            {(agentAnswer || agentLoading || agentEvents.length > 0) && (
              <div className="agent-response-card">
                <div className="agent-response-header">
                  <div className="agent-response-title">
                    <Sparkles size={20} color="var(--primary)" />
                    AI Response
                  </div>

                  {agentEvents.length > 0 && (
                    <button
                      type="button"
                      className="btn-event-stream"
                      onClick={() => setShowStreamEvents(!showStreamEvents)}
                    >
                      <Terminal size={16} />
                      {showStreamEvents ? 'Hide Stream Events' : 'Inspect Stream Events (pretty_print_event)'}
                    </button>
                  )}
                </div>

                {agentLoading && !agentAnswer ? (
                  <div style={{ padding: '1.5rem', textAlign: 'center', color: 'var(--text-muted)' }}>
                    <RefreshCw className="spinner" size={24} style={{ marginBottom: '0.5rem', color: 'var(--primary)' }} />
                    <p style={{ fontSize: '0.9rem' }}>Streaming message events from agent state graph...</p>
                  </div>
                ) : (
                  <div className="agent-response-content">
                    {agentAnswer || "Processing query response..."}
                  </div>
                )}

                {/* Pretty Print Event Stream Drawer / View */}
                {showStreamEvents && (
                  <PrettyPrintEventStream events={agentEvents} />
                )}
              </div>
            )}
          </div>
        )}

        {/* Right Section - Existing Collections Sidebar */}
        <div className="glass-card">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem' }}>
            <h3 style={{ fontSize: '1.1rem', fontWeight: 600, color: '#fff', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <Layers size={18} color="var(--accent-cyan)" />
              Vector Collections
            </h3>
            <button onClick={fetchCollections} className="refresh-btn" title="Refresh list">
              <RefreshCw size={14} className={apiStatus === 'checking' ? 'spinner' : ''} />
            </button>
          </div>

          {collections.length === 0 ? (
            <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', textAlign: 'center', padding: '2rem 0' }}>
              No collections found in ChromaDB yet. Ingest your first PDF document to create one!
            </p>
          ) : (
            <div>
              {collections.map((col) => (
                <div 
                  key={col} 
                  className="collection-card-item"
                  style={{
                    borderColor: (queryCollection === col || selectedCollection === col || agentCollection === col) ? 'var(--primary)' : 'var(--border-color)',
                    cursor: 'pointer'
                  }}
                  onClick={() => {
                    setSelectedCollection(col);
                    setQueryCollection(col);
                    setAgentCollection(col);
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
                    <Server size={16} color="var(--primary)" />
                    <span style={{ fontWeight: 500, fontSize: '0.9rem' }}>{col}</span>
                  </div>
                  <span className="collection-badge">Active</span>
                </div>
              ))}
            </div>
          )}

          <div style={{ marginTop: '2rem', padding: '1rem', background: 'rgba(255,255,255,0.03)', borderRadius: 'var(--radius-md)', border: '1px solid var(--border-color)' }}>
            <h4 style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-muted)', marginBottom: '0.5rem' }}>
              💡 Quick Info
            </h4>
            <p style={{ fontSize: '0.8rem', color: 'var(--text-dim)', lineHeight: 1.4 }}>
              Use the <strong>Agentic RAG Assistant</strong> tab to ask questions to any collection and view real-time streamed graph events with <code>pretty_print_event</code> formatting!
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
