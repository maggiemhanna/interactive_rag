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
  Search,
  Sparkles,
  ChevronDown,
  ChevronUp,
  Bot,
  Code2,
  Copy,
  Check,
  Trash2,
  AlertTriangle,
  LogOut,
  User,
  AlignLeft,
  Highlighter,
  BookOpen,
  Zap,
  MessageSquare,
  MessageSquarePlus,
  RotateCcw,
  Send,
  CornerDownLeft,
  Square,
  History,
  ArrowLeft,
  Shield
} from 'lucide-react';
import {
  API_BASE,
  AGENT_API_BASE,
  DEFAULT_ALLOWED_LLM_MODELS,
  DEFAULT_ALLOWED_EMBEDDING_MODELS,
  subscribeToAuthChanges,
  logoutUser,
} from './firebase';
import {
  fetchUserDataInventory,
  eraseAllUserData,
  formatStorageBytes,
} from './api';
import AuthGate from './AuthGate';

const QUERY_STOPWORDS = new Set([
  'the', 'and', 'for', 'that', 'with', 'this', 'from', 'what', 'how', 'why',
  'who', 'when', 'where', 'which', 'are', 'was', 'were', 'been', 'being',
  'have', 'has', 'had', 'does', 'did', 'can', 'could', 'will', 'would',
  'should', 'may', 'might', 'into', 'about', 'their', 'there', 'these',
  'those', 'than', 'then', 'them', 'they', 'your', 'our', 'its', 'his',
  'her', 'not', 'but', 'all', 'any', 'some', 'such', 'more', 'most', 'other',
  'over', 'under', 'between', 'through', 'during', 'before', 'after',
  'les', 'des', 'dans', 'pour', 'avec', 'une', 'sur', 'par', 'plus', 'est', 'sont'
]);

const CHUNK_BULLET_RE = /^([•●▪◦‣⁃]|\u2022|[-–—*])\s+(.*)$/;
const CHUNK_NUMBERED_RE = /^(\d+(?:\.\d+)*[.)]|\([a-zA-Z0-9]+\))\s+([A-Z0-9"“‘(\[].*)$/;
const CHUNK_ABBREV_RE = /\b(?:e\.g|i\.e|et\s+al|vs|cf|viz|Dr|Mr|Mrs|Ms|Prof|Fig|No|Vol|pp?|Inc|Ltd|Co|Jr|Sr|St)\.\s*$/i;
const CHUNK_SENTENCE_END_RE = /[.!?]["'”’)\]]*\s*$/;
const CHUNK_HEADING_PREFIX_RE = /^(?:Standard|Annex|Chapter|Section|Table|Figure|Box|Part|Module|Appendix)\s+[A-Z0-9]+/i;
const CHUNK_DANGLING_WORD_RE = /\b(?:and|or|of|to|in|for|with|on|by|at|from|as|the|a|an|that|which|who|whose|their|its|our|your|his|her|this|these|those|are|is|was|were|be|been|have|has|had|can|may|will|would|should|could|into|over|under|between|through|during|before|after)$/i;

function parseChunkBlocks(rawText) {
  if (!rawText) return [];
  const normalized = rawText.replace(/\r\n/g, '\n').trim();
  if (!normalized) return [];

  const rawLines = normalized.split('\n');
  const nonEmptyLengths = rawLines
    .map((l) => l.trim().length)
    .filter((len) => len > 20)
    .sort((a, b) => a - b);
  const typicalWidth =
    nonEmptyLengths.length > 0
      ? nonEmptyLengths[Math.floor(nonEmptyLengths.length * 0.8)]
      : 80;

  const blocks = [];
  let current = null;

  const flush = () => {
    if (current && current.text.trim()) {
      blocks.push({
        type: current.type,
        marker: current.marker || null,
        text: current.text.replace(/\s{2,}/g, ' ').trim(),
      });
    }
    current = null;
  };

  for (let i = 0; i < rawLines.length; i++) {
    const line = rawLines[i].trim();
    if (!line) {
      flush();
      continue;
    }

    const bulletMatch = line.match(CHUNK_BULLET_RE);
    if (bulletMatch) {
      flush();
      current = { type: 'bullet', marker: bulletMatch[1], text: bulletMatch[2] };
    } else {
      const numMatch = line.match(CHUNK_NUMBERED_RE);
      if (numMatch) {
        flush();
        current = { type: 'bullet', marker: numMatch[1], text: numMatch[2] };
      } else if (!current) {
        const nextLine = (rawLines[i + 1] || '').trim();
        const isExplicitHeading =
          CHUNK_HEADING_PREFIX_RE.test(line) &&
          line.length < typicalWidth * 0.85 &&
          !CHUNK_SENTENCE_END_RE.test(line);
        const isShortHeading =
          (i > 0 || CHUNK_BULLET_RE.test(nextLine)) &&
          Boolean(nextLine) &&
          !CHUNK_SENTENCE_END_RE.test(line) &&
          !/[,;:\-–—]$/.test(line) &&
          !CHUNK_DANGLING_WORD_RE.test(line) &&
          /^[A-Z0-9]/.test(line) &&
          /^[A-Z0-9•●▪◦\-]/.test(nextLine) &&
          line.length < typicalWidth * 0.68;

        if (isExplicitHeading || isShortHeading) {
          blocks.push({ type: 'heading', marker: null, text: line });
          continue;
        }

        current = { type: 'paragraph', marker: null, text: line };
      } else if (/[a-zA-Z0-9]-$/.test(current.text) && /^[a-z0-9]/.test(line)) {
        current.text = current.text + line;
      } else {
        current.text = `${current.text} ${line}`;
      }
    }

    const nextNonEmpty = (rawLines[i + 1] || '').trim();
    if (!current || !nextNonEmpty) continue;

    if (
      current.type === 'paragraph' &&
      CHUNK_SENTENCE_END_RE.test(line) &&
      !CHUNK_ABBREV_RE.test(line) &&
      /^[A-Z"“‘(\[0-9]/.test(nextNonEmpty)
    ) {
      flush();
    } else if (
      current.type === 'bullet' &&
      !CHUNK_BULLET_RE.test(nextNonEmpty) &&
      !CHUNK_NUMBERED_RE.test(nextNonEmpty) &&
      CHUNK_SENTENCE_END_RE.test(line) &&
      !CHUNK_ABBREV_RE.test(line) &&
      /^[A-Z"“‘(\[]/.test(nextNonEmpty) &&
      line.length < typicalWidth * 0.85
    ) {
      flush();
    }
  }

  flush();
  return blocks;
}

function formatChunkPlainText(rawText) {
  const blocks = parseChunkBlocks(rawText);
  if (blocks.length === 0) return rawText || '';
  return blocks
    .map((b) => (b.type === 'bullet' ? `${b.marker} ${b.text}` : b.text))
    .join('\n\n');
}

function extractQueryTerms(query) {
  if (!query) return [];
  const rawTokens = query.split(/[^a-zA-Z0-9À-ÿ]+/).filter(Boolean);
  const seen = new Set();
  const terms = [];

  for (const token of rawTokens) {
    const lower = token.toLowerCase();
    const isAcronym = /^[A-Z0-9]{2,}$/.test(token);
    if ((token.length >= 3 || isAcronym) && !QUERY_STOPWORDS.has(lower) && !seen.has(lower)) {
      seen.add(lower);
      terms.push(token);
    }
  }

  return terms.sort((a, b) => b.length - a.length);
}

function renderHighlightedText(text, terms) {
  if (!text || !terms || terms.length === 0) return text;
  const escaped = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  let regex;
  try {
    regex = new RegExp(
      `((?<![a-zA-Z0-9À-ÿ])(?:${escaped.join('|')})(?:s|es|ed|ing)?(?![a-zA-Z0-9À-ÿ]))`,
      'gi'
    );
  } catch {
    regex = new RegExp(`(\\b(?:${escaped.join('|')})(?:s|es|ed|ing)?\\b)`, 'gi');
  }

  const parts = text.split(regex);
  if (parts.length <= 1) return text;

  return parts.map((part, idx) =>
    idx % 2 === 1 ? (
      <mark key={idx} className="chunk-highlight">
        {part}
      </mark>
    ) : (
      part
    )
  );
}

function FormattedChunkView({ rawText, queryTerms, viewMode, highlightEnabled }) {
  const activeTerms = highlightEnabled ? queryTerms : [];

  if (viewMode === 'raw') {
    return (
      <div className="result-content result-content-raw">
        {renderHighlightedText(rawText, activeTerms)}
      </div>
    );
  }

  const blocks = parseChunkBlocks(rawText);
  const trimmed = (rawText || '').trim();
  const startsMidSentence = /^[a-z,;:\])]/.test(trimmed);
  const lastBlock = blocks[blocks.length - 1];
  const endsMidSentence = Boolean(
    lastBlock &&
      lastBlock.type !== 'heading' &&
      /[a-zA-Z0-9,;:\-–—(]$/.test(trimmed)
  );

  return (
    <div className="result-content result-content-reflowed">
      {blocks.map((block, bIdx) => {
        const isFirst = bIdx === 0;
        const isLast = bIdx === blocks.length - 1;
        const showLeadEllipsis = isFirst && startsMidSentence && block.type === 'paragraph';
        const showTrailEllipsis = isLast && endsMidSentence;

        if (block.type === 'heading') {
          return (
            <div key={bIdx} className="chunk-heading">
              {renderHighlightedText(block.text, activeTerms)}
            </div>
          );
        }

        if (block.type === 'bullet') {
          return (
            <div key={bIdx} className="chunk-bullet-item">
              <span className="chunk-bullet-marker">{block.marker}</span>
              <span className="chunk-bullet-body">
                {renderHighlightedText(block.text, activeTerms)}
                {showTrailEllipsis && (
                  <span className="chunk-ellipsis" title="Chunk continues in next segment">
                    …
                  </span>
                )}
              </span>
            </div>
          );
        }

        return (
          <p key={bIdx} className="chunk-paragraph">
            {showLeadEllipsis && (
              <span className="chunk-ellipsis" title="Chunk begins mid-sentence">
                …
              </span>
            )}
            {renderHighlightedText(block.text, activeTerms)}
            {showTrailEllipsis && (
              <span className="chunk-ellipsis" title="Chunk continues in next segment">
                …
              </span>
            )}
          </p>
        );
      })}
    </div>
  );
}

function unescapeXml(str) {
  return (str || '')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&');
}

function parseRetrievedChunksXml(xmlText) {
  if (typeof xmlText !== 'string' || !xmlText.includes('<retrieved_chunk')) {
    return [];
  }
  const chunks = [];
  const chunkRe = /<retrieved_chunk\s+source="([^"]*)"\s+page="([^"]*)">\s*([\s\S]*?)\s*<\/retrieved_chunk>/g;
  let m;
  while ((m = chunkRe.exec(xmlText)) !== null) {
    chunks.push({
      source: unescapeXml(m[1]).split(/[\\/]/).pop().trim(),
      page: unescapeXml(m[2]).trim(),
      content: unescapeXml(m[3]),
    });
  }
  return chunks;
}

function extractRetrievedChunksFromEvents(events) {
  if (!Array.isArray(events)) return [];
  const all = [];
  const seen = new Set();

  for (const evt of events) {
    const msgType = (evt?.type || '').toLowerCase();
    if ((msgType === 'tool' || msgType === 'function') && typeof evt.content === 'string') {
      for (const chunk of parseRetrievedChunksXml(evt.content)) {
        const dedupKey = `${chunk.source}::${chunk.page}::${chunk.content.slice(0, 120)}`;
        if (!seen.has(dedupKey)) {
          seen.add(dedupKey);
          all.push(chunk);
        }
      }
    }
  }
  return all;
}

function isCitationInner(inner) {
  if (!inner || inner.length > 180) return false;
  if (/^source\s*[:=]/i.test(inner.trim())) return true;
  if (/\b[\w.\-]+\.(?:pdf|docx?|txt|md|csv|html?)\b/i.test(inner)) return true;
  if (/^[^\[\]]+?,\s*(?:pp?\.?|pages?)\s*\d+/i.test(inner)) return true;
  return false;
}

function parseCitationSegment(rawSegment) {
  const cleaned = rawSegment.trim().replace(/^source\s*[:=]\s*/i, '');
  if (!cleaned) return null;

  const commaIdx = cleaned.indexOf(',');
  if (commaIdx === -1) {
    const srcOnly = cleaned.replace(/^["']|["']$/g, '').split(/[\\/]/).pop().trim();
    return { source: srcOnly, pages: [], pageLabel: null };
  }

  const rawSource = cleaned.slice(0, commaIdx).replace(/^["']|["']$/g, '').trim();
  const source = rawSource.split(/[\\/]/).pop().trim();
  const rest = cleaned.slice(commaIdx + 1).trim();

  const pageGroupRe = /^(?:pp?\.?|pages?\s*[:=]?)\s*["']?(\d+(?:\s*[-–—]\s*\d+)?(?:\s*,\s*(?:pp?\.?\s*)?\d+(?:\s*[-–—]\s*\d+)?)*)["']?(.*)$/i;
  const groupMatch = rest.match(pageGroupRe);
  if (groupMatch) {
    const rawPageList = groupMatch[1];
    const trailingDetail = (groupMatch[2] || '').replace(/^[,\s]+/, '').trim();
    const nums = (rawPageList.match(/\d+(?:\s*[-–—]\s*\d+)?/g) || []).map((n) =>
      n.replace(/\s+/g, '')
    );
    const uniquePages = [...new Set(nums)];
    const prefix =
      uniquePages.length > 1 || uniquePages.some((p) => /[-–—]/.test(p))
        ? 'pp.'
        : 'p.';
    const baseLabel = `${prefix} ${uniquePages.join(', ')}`;
    return {
      source,
      pages: uniquePages,
      pageLabel: trailingDetail ? `${baseLabel}, ${trailingDetail}` : baseLabel,
    };
  }

  return { source, pages: [], pageLabel: rest || null };
}

function parseCitationBracket(inner) {
  return inner
    .split(/\s*(?:;\s*|,\s*(?=[a-zA-Z0-9._-]+\.(?:pdf|docx?|txt|md|csv|html?)\b))/)
    .map(parseCitationSegment)
    .filter((c) => c && c.source);
}

function collectAllCitations(markdown) {
  if (!markdown) return [];
  const map = new Map();
  const bracketRe =
    /\[([^\[\]\n]+)\](?!\()|\(([\w.\-]+\.(?:pdf|docx?|txt|md|csv|html?)\s*,\s*(?:pp?\.?|pages?)\s*[^()\n]+)\)/g;
  let m;
  while ((m = bracketRe.exec(markdown)) !== null) {
    const inner = m[1] || m[2];
    if (!isCitationInner(inner)) continue;
    for (const cit of parseCitationBracket(inner)) {
      if (!map.has(cit.source)) {
        map.set(cit.source, new Set());
      }
      const pageSet = map.get(cit.source);
      for (const p of cit.pages) pageSet.add(p);
    }
  }
  return Array.from(map.entries()).map(([source, pageSet]) => ({
    source,
    pages: Array.from(pageSet),
  }));
}

function pageMatchesCitedRange(chunkPage, citedToken) {
  if (!chunkPage || !citedToken) return false;
  if (String(chunkPage).trim() === String(citedToken).trim()) return true;
  const rangeMatch = String(citedToken).match(/^(\d+)\s*[-–—]\s*(\d+)$/);
  if (rangeMatch) {
    const num = Number(chunkPage);
    const start = Number(rangeMatch[1]);
    const end = Number(rangeMatch[2]);
    if (!Number.isNaN(num) && num >= start && num <= end) return true;
  }
  return false;
}

function findMatchingChunksForCitation(citation, retrievedChunks) {
  if (!citation?.source || !retrievedChunks?.length) return null;
  const sameSource = retrievedChunks.filter(
    (c) => c.source.toLowerCase() === citation.source.toLowerCase()
  );
  if (sameSource.length === 0) return null;

  if (citation.pages && citation.pages.length > 0) {
    const exact = sameSource.filter((c) =>
      citation.pages.some((p) => pageMatchesCitedRange(c.page, p))
    );
    if (exact.length > 0) {
      return { exact: true, chunks: exact };
    }
  }
  return { exact: false, chunks: sameSource };
}

const INLINE_MARKDOWN_MASTER_RE =
  /`([^`\n]+)`|\[([^\[\]\n]+)\]\((https?:\/\/[^\s)]+)\)|\[([^\[\]\n]+)\](?!\()|\(([\w.\-]+\.(?:pdf|docx?|txt|md|csv|html?)\s*,\s*(?:pp?\.?|pages?)\s*[^()\n]+)\)|\*\*\*([^\n]+?)\*\*\*|\*\*([^\n]+?)\*\*|__([^\n]+?)__|(?<!\*)\*([^\s*](?:[^\n*]*?[^\s*])?)\*(?!\*)|(?<![a-zA-Z0-9_])_([^\s_](?:[^\n_]*?[^\s_])?)_(?![a-zA-Z0-9_])/g;

function renderInlineMarkdown(text, options = {}, keyPrefix = 'in') {
  if (!text) return null;
  const { retrievedChunks = [], activeCitationKey = null, onSelectCitation = null } = options;

  const nodes = [];
  let lastIdx = 0;
  const re = new RegExp(INLINE_MARKDOWN_MASTER_RE.source, 'g');
  let m;
  let tokenIdx = 0;

  while ((m = re.exec(text)) !== null) {
    if (m.index > lastIdx) {
      nodes.push(text.slice(lastIdx, m.index));
    }

    const key = `${keyPrefix}-${tokenIdx++}`;

    if (m[1] !== undefined) {
      nodes.push(
        <code key={key} className="md-inline-code">
          {m[1]}
        </code>
      );
    } else if (m[2] !== undefined) {
      nodes.push(
        <a
          key={key}
          href={m[3]}
          target="_blank"
          rel="noopener noreferrer"
          className="md-link"
        >
          {renderInlineMarkdown(m[2], options, `${key}-lnk`)}
        </a>
      );
    } else if (m[4] !== undefined || m[5] !== undefined) {
      const inner = m[4] !== undefined ? m[4] : m[5];
      if (isCitationInner(inner)) {
        const parsedCitations = parseCitationBracket(inner);
        nodes.push(
          <span key={key} className="md-citation-group">
            {parsedCitations.map((cit, cIdx) => {
              const citKey = `${cit.source}::${(cit.pages || []).join(',')}`;
              const matchInfo = findMatchingChunksForCitation(cit, retrievedChunks);
              const isInteractive = Boolean(onSelectCitation && matchInfo);
              const isActive = activeCitationKey === citKey;

              const badgeInner = (
                <>
                  <FileText size={11} className="md-citation-icon" />
                  <span className="md-citation-source">{cit.source}</span>
                  {cit.pageLabel && (
                    <span className="md-citation-page">{cit.pageLabel}</span>
                  )}
                </>
              );

              if (isInteractive) {
                return (
                  <button
                    key={`${key}-c-${cIdx}`}
                    type="button"
                    className={`md-citation-badge interactive ${isActive ? 'active' : ''}`}
                    onClick={() => onSelectCitation(cit, citKey)}
                    title={`${isActive ? 'Hide' : 'View'} source excerpt from ${cit.source}${cit.pageLabel ? ` (${cit.pageLabel})` : ''}`}
                  >
                    {badgeInner}
                  </button>
                );
              }

              return (
                <span key={`${key}-c-${cIdx}`} className="md-citation-badge">
                  {badgeInner}
                </span>
              );
            })}
          </span>
        );
      } else {
        const open = m[4] !== undefined ? '[' : '(';
        const close = m[4] !== undefined ? ']' : ')';
        nodes.push(
          <React.Fragment key={key}>
            {open}
            {renderInlineMarkdown(inner, options, `${key}-brk`)}
            {close}
          </React.Fragment>
        );
      }
    } else if (m[6] !== undefined) {
      nodes.push(
        <strong key={key} className="md-strong">
          <em className="md-em">{renderInlineMarkdown(m[6], options, `${key}-bi`)}</em>
        </strong>
      );
    } else if (m[7] !== undefined || m[8] !== undefined) {
      nodes.push(
        <strong key={key} className="md-strong">
          {renderInlineMarkdown(m[7] ?? m[8], options, `${key}-b`)}
        </strong>
      );
    } else if (m[9] !== undefined || m[10] !== undefined) {
      nodes.push(
        <em key={key} className="md-em">
          {renderInlineMarkdown(m[9] ?? m[10], options, `${key}-i`)}
        </em>
      );
    }

    lastIdx = re.lastIndex;
  }

  if (lastIdx < text.length) {
    nodes.push(text.slice(lastIdx));
  }

  return nodes;
}

function parseAgentMarkdownBlocks(markdown) {
  if (!markdown) return [];
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const blocks = [];
  let i = 0;

  const HEADING_RE = /^\s*(#{1,4})\s+(.+?)\s*$/;
  const HR_RE = /^\s*(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$/;
  const UL_RE = /^(\s*)([*+\-•●▪◦])\s+(.+)$/;
  const OL_RE = /^(\s*)(\d+[.)])\s+(.+)$/;
  const TABLE_SEP_RE = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$/;

  const splitTableRow = (row) => {
    let trimmed = row.trim();
    if (trimmed.startsWith('|')) trimmed = trimmed.slice(1);
    if (trimmed.endsWith('|')) trimmed = trimmed.slice(0, -1);
    return trimmed.split('|').map((c) => c.trim());
  };

  while (i < lines.length) {
    const rawLine = lines[i];
    const trimmed = rawLine.trim();

    if (!trimmed) {
      i++;
      continue;
    }

    if (trimmed.startsWith('```')) {
      const lang = trimmed.slice(3).trim();
      i++;
      const codeLines = [];
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        codeLines.push(lines[i]);
        i++;
      }
      if (i < lines.length) i++;
      blocks.push({ type: 'code', lang, content: codeLines.join('\n') });
      continue;
    }

    if (trimmed.includes('|') && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
      const headers = splitTableRow(rawLine);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim().includes('|') && lines[i].trim() !== '') {
        rows.push(splitTableRow(lines[i]));
        i++;
      }
      blocks.push({ type: 'table', headers, rows });
      continue;
    }

    const headingMatch = rawLine.match(HEADING_RE);
    if (headingMatch) {
      blocks.push({
        type: 'heading',
        level: headingMatch[1].length,
        text: headingMatch[2],
      });
      i++;
      continue;
    }

    if (HR_RE.test(trimmed)) {
      blocks.push({ type: 'hr' });
      i++;
      continue;
    }

    if (/^\s*>\s?/.test(rawLine)) {
      const quoteLines = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quoteLines.push(lines[i].replace(/^\s*>\s?/, '').trim());
        i++;
      }
      blocks.push({ type: 'blockquote', text: quoteLines.join(' ') });
      continue;
    }

    if (UL_RE.test(rawLine) || OL_RE.test(rawLine)) {
      const items = [];
      while (i < lines.length) {
        const l = lines[i];
        if (!l.trim()) {
          if (i + 1 < lines.length && (UL_RE.test(lines[i + 1]) || OL_RE.test(lines[i + 1]))) {
            i++;
            continue;
          }
          break;
        }
        if (HR_RE.test(l.trim())) break;

        const ul = l.match(UL_RE);
        const ol = !ul ? l.match(OL_RE) : null;
        if (ul || ol) {
          const indentSpaces = (ul ? ul[1] : ol[1]).replace(/\t/g, '  ').length;
          const level = Math.min(2, Math.floor(indentSpaces / 2));
          items.push({
            ordered: Boolean(ol),
            marker: ol ? ol[2] : '•',
            level,
            text: (ul ? ul[3] : ol[3]).trim(),
          });
          i++;
        } else if (
          items.length > 0 &&
          !HEADING_RE.test(l) &&
          !l.trim().startsWith('```') &&
          !/^\s*>\s?/.test(l)
        ) {
          items[items.length - 1].text += ` ${l.trim()}`;
          i++;
        } else {
          break;
        }
      }
      blocks.push({ type: 'list', items });
      continue;
    }

    const paraLines = [];
    while (
      i < lines.length &&
      lines[i].trim() !== '' &&
      !lines[i].trim().startsWith('```') &&
      !HEADING_RE.test(lines[i]) &&
      !HR_RE.test(lines[i].trim()) &&
      !/^\s*>\s?/.test(lines[i]) &&
      !UL_RE.test(lines[i]) &&
      !OL_RE.test(lines[i]) &&
      !(lines[i].trim().includes('|') && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1]))
    ) {
      paraLines.push(lines[i].trim());
      i++;
    }
    blocks.push({ type: 'paragraph', text: paraLines.join(' ') });
  }

  return blocks;
}

function AgentMarkdownView({
  markdown,
  events = [],
  viewMode = 'rendered',
  queryTerms = [],
  compact = false,
}) {
  const [selectedCitation, setSelectedCitation] = useState(null);

  useEffect(() => {
    setSelectedCitation(null);
  }, [markdown]);

  if (!markdown) {
    return (
      <div className="agent-response-content">
        <span style={{ color: 'var(--text-muted)' }}>Preparing answer...</span>
      </div>
    );
  }

  if (viewMode === 'raw') {
    return <pre className="agent-response-raw">{markdown}</pre>;
  }

  const retrievedChunks = extractRetrievedChunksFromEvents(events);
  const blocks = parseAgentMarkdownBlocks(markdown);
  const citationsSummary = compact ? [] : collectAllCitations(markdown);

  const handleSelectCitation = (cit, citKey) => {
    setSelectedCitation((prev) =>
      prev && prev.key === citKey ? null : { citation: cit, key: citKey }
    );
  };

  const inlineOptions = {
    retrievedChunks,
    activeCitationKey: selectedCitation?.key || null,
    onSelectCitation: compact ? null : handleSelectCitation,
  };

  const previewMatch =
    !compact && selectedCitation
      ? findMatchingChunksForCitation(selectedCitation.citation, retrievedChunks)
      : null;

  return (
    <div className={compact ? 'agent-md-compact' : 'agent-response-content'}>
      <div className="agent-md-body">
        {blocks.map((block, idx) => {
          if (block.type === 'heading') {
            const Tag = block.level <= 2 ? 'h3' : 'h4';
            return (
              <Tag key={idx} className={`md-heading md-h${block.level}`}>
                {renderInlineMarkdown(block.text, inlineOptions, `b${idx}`)}
              </Tag>
            );
          }

          if (block.type === 'hr') {
            return <hr key={idx} className="md-hr" />;
          }

          if (block.type === 'code') {
            return (
              <div key={idx} className="md-code-block-wrap">
                {block.lang && <div className="md-code-lang">{block.lang}</div>}
                <pre className="md-code-block">
                  <code>{block.content}</code>
                </pre>
              </div>
            );
          }

          if (block.type === 'blockquote') {
            return (
              <blockquote key={idx} className="md-blockquote">
                {renderInlineMarkdown(block.text, inlineOptions, `b${idx}`)}
              </blockquote>
            );
          }

          if (block.type === 'table') {
            return (
              <div key={idx} className="md-table-wrapper">
                <table className="md-table">
                  <thead>
                    <tr>
                      {block.headers.map((h, hIdx) => (
                        <th key={hIdx}>
                          {renderInlineMarkdown(h, inlineOptions, `b${idx}-th${hIdx}`)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, rIdx) => (
                      <tr key={rIdx}>
                        {row.map((cell, cIdx) => (
                          <td key={cIdx}>
                            {renderInlineMarkdown(
                              cell,
                              inlineOptions,
                              `b${idx}-r${rIdx}-c${cIdx}`
                            )}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          }

          if (block.type === 'list') {
            return (
              <ul key={idx} className="md-list">
                {block.items.map((item, iIdx) => (
                  <li
                    key={iIdx}
                    className={`md-list-item md-indent-${item.level} ${item.ordered ? 'ordered' : 'unordered'}`}
                  >
                    <span className="md-list-marker">
                      {item.ordered ? item.marker : item.level > 0 ? '◦' : '•'}
                    </span>
                    <div className="md-list-text">
                      {renderInlineMarkdown(
                        item.text,
                        inlineOptions,
                        `b${idx}-li${iIdx}`
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            );
          }

          return (
            <p key={idx} className="md-paragraph">
              {renderInlineMarkdown(block.text, inlineOptions, `b${idx}`)}
            </p>
          );
        })}
      </div>

      {citationsSummary.length > 0 && (
        <div className="agent-sources-footer">
          <div className="agent-sources-header">
            <span className="agent-sources-label">
              <BookOpen size={13} />
              Sources ({citationsSummary.length})
            </span>
            {retrievedChunks.length > 0 && (
              <span className="agent-sources-hint">
                Click any citation or page number to view the source excerpt
              </span>
            )}
          </div>

          <div className="agent-sources-list">
            {citationsSummary.map((srcItem, sIdx) => {
              const sourceMatch = findMatchingChunksForCitation(
                { source: srcItem.source, pages: [] },
                retrievedChunks
              );

              return (
                <div key={sIdx} className="agent-source-group">
                  <FileText size={13} className="agent-source-group-icon" />
                  <span className="agent-source-group-name">{srcItem.source}</span>

                  {srcItem.pages.length > 0 ? (
                    <div className="agent-source-pages">
                      {srcItem.pages.map((p) => {
                        const pageCit = {
                          source: srcItem.source,
                          pages: [p],
                          pageLabel: `p. ${p}`,
                        };
                        const pageKey = `${srcItem.source}::${p}`;
                        const canPreview = Boolean(sourceMatch);
                        const isPageActive = selectedCitation?.key === pageKey;

                        return canPreview ? (
                          <button
                            key={p}
                            type="button"
                            className={`agent-source-page-btn ${isPageActive ? 'active' : ''}`}
                            onClick={() => handleSelectCitation(pageCit, pageKey)}
                            title={`View excerpt from ${srcItem.source} (p. ${p})`}
                          >
                            p. {p}
                          </button>
                        ) : (
                          <span key={p} className="agent-source-page-static">
                            p. {p}
                          </span>
                        );
                      })}
                    </div>
                  ) : (
                    sourceMatch && (
                      <button
                        type="button"
                        className={`agent-source-page-btn ${
                          selectedCitation?.key === `${srcItem.source}::` ? 'active' : ''
                        }`}
                        onClick={() =>
                          handleSelectCitation(
                            { source: srcItem.source, pages: [], pageLabel: null },
                            `${srcItem.source}::`
                          )
                        }
                      >
                        View excerpt
                      </button>
                    )
                  )}
                </div>
              );
            })}
          </div>

          {previewMatch && selectedCitation && (
            <div className="agent-citation-preview">
              <div className="agent-citation-preview-header">
                <div className="agent-citation-preview-title">
                  <BookOpen size={14} color="var(--accent-cyan)" />
                  <span>Source Excerpt — {selectedCitation.citation.source}</span>
                  {selectedCitation.citation.pageLabel && (
                    <span className="result-page-badge">
                      {selectedCitation.citation.pageLabel}
                    </span>
                  )}
                  {!previewMatch.exact && (
                    <span className="agent-citation-fallback-note">
                      (matching excerpts from this document)
                    </span>
                  )}
                </div>
                <button
                  type="button"
                  className="remove-file-btn"
                  onClick={() => setSelectedCitation(null)}
                  title="Close preview"
                >
                  <X size={15} />
                </button>
              </div>

              <div className="agent-citation-preview-list">
                {previewMatch.chunks.map((chunk, cIdx) => (
                  <div key={cIdx} className="agent-citation-chunk-item">
                    {previewMatch.chunks.length > 1 || !previewMatch.exact ? (
                      <div className="agent-citation-chunk-meta">
                        <span>Excerpt #{cIdx + 1}</span>
                        <span className="result-page-badge">p. {chunk.page}</span>
                      </div>
                    ) : null}
                    <FormattedChunkView
                      rawText={chunk.content}
                      queryTerms={queryTerms}
                      viewMode="reflowed"
                      highlightEnabled={queryTerms.length > 0}
                    />
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const STARTER_PROMPTS = [
  'Summarize the main topics and key takeaways across these documents.',
  'What are the main risks, challenges, or limitations discussed?',
  'List the key recommendations or conclusions with page references.',
];

function createThreadId() {
  const rand = Math.random().toString(36).slice(2, 7);
  return `thread-${Date.now().toString(36)}-${rand}`;
}

function createConversationThread(collectionName = '') {
  const now = Date.now();
  return {
    id: createThreadId(),
    collection: collectionName || '',
    title: 'New Chat',
    createdAt: now,
    updatedAt: now,
    turns: [],
  };
}

function summarizeThreadTitle(query) {
  const cleaned = (query || '').replace(/\s+/g, ' ').trim();
  if (!cleaned) return 'New Chat';
  return cleaned.length > 48 ? `${cleaned.slice(0, 47).trimEnd()}…` : cleaned;
}

function getThreadStorageKey(uid) {
  return `irag_agent_threads_v1_${uid || 'anon'}`;
}

function loadStoredThreads(uid) {
  if (!uid || typeof window === 'undefined') return null;
  try {
    const raw = window.sessionStorage.getItem(getThreadStorageKey(uid));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || !Array.isArray(parsed.threads) || parsed.threads.length === 0) {
      return null;
    }
    const sanitizedThreads = parsed.threads
      .filter((t) => t && typeof t === 'object')
      .map((t) => ({
        ...t,
        id: typeof t.id === 'string' && t.id ? t.id : createThreadId(),
        title: typeof t.title === 'string' && t.title ? t.title : 'New Chat',
        collection: typeof t.collection === 'string' ? t.collection : '',
        turns: Array.isArray(t.turns)
          ? t.turns
              .filter((trn) => trn && typeof trn === 'object')
              .map((trn) => {
                const normalizedTurn = {
                  ...trn,
                  query: typeof trn.query === 'string' ? trn.query : String(trn.query ?? ''),
                  answer: typeof trn.answer === 'string' ? trn.answer : String(trn.answer ?? ''),
                  events: Array.isArray(trn.events) ? trn.events : [],
                };
                return normalizedTurn.status === 'streaming'
                  ? {
                      ...normalizedTurn,
                      status: normalizedTurn.answer ? 'done' : 'error',
                      error: normalizedTurn.answer
                        ? null
                        : 'Response interrupted when the page reloaded.',
                    }
                  : normalizedTurn;
              })
          : [],
      }));
    if (sanitizedThreads.length === 0) return null;
    return {
      threads: sanitizedThreads,
      activeThreadId: parsed.activeThreadId || sanitizedThreads[0].id,
    };
  } catch (e) {
    console.warn('Could not restore session threads:', e);
    return null;
  }
}

function formatTurnTime(timestamp) {
  if (!timestamp) return '';
  try {
    return new Date(timestamp).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return '';
  }
}

function summarizeTurnRetrieval(events) {
  let toolCallsCount = 0;
  let chunksCount = 0;
  let lastToolQuery = null;

  if (Array.isArray(events)) {
    for (const evt of events) {
      const msgType = (evt?.type || '').toLowerCase();
      if ((msgType === 'ai' || msgType === 'assistant') && Array.isArray(evt.tool_calls)) {
        toolCallsCount += evt.tool_calls.length;
        for (const call of evt.tool_calls) {
          if (call?.args?.query) {
            lastToolQuery = call.args.query;
          }
        }
      } else if (msgType === 'tool' || msgType === 'function') {
        chunksCount += parseRetrievedChunksXml(evt.content).length;
      }
    }
  }

  return { toolCallsCount, chunksCount, lastToolQuery };
}

function describeLiveAgentStep(events, endpointMode, turnNumber) {
  if (endpointMode === 'standard') {
    return {
      stage: 'Searching your documents and writing an answer…',
      detail: 'Please wait a moment',
    };
  }

  if (!Array.isArray(events) || events.length === 0) {
    return {
      stage: 'Thinking…',
      detail:
        turnNumber > 1
          ? 'Reviewing your earlier messages'
          : 'Preparing to search your documents',
    };
  }

  let latestToolQuery = null;
  let totalChunks = 0;
  let sawToolResult = false;

  for (const evt of events) {
    const t = (evt?.type || '').toLowerCase();
    if (
      (t === 'ai' || t === 'assistant') &&
      Array.isArray(evt.tool_calls) &&
      evt.tool_calls.length > 0
    ) {
      const lastCall = evt.tool_calls[evt.tool_calls.length - 1];
      latestToolQuery = lastCall?.args?.query || null;
      sawToolResult = false;
    } else if (t === 'tool' || t === 'function') {
      sawToolResult = true;
      totalChunks += parseRetrievedChunksXml(evt.content).length;
    }
  }

  if (sawToolResult) {
    return {
      stage:
        totalChunks > 0
          ? `Writing answer using ${totalChunks} matching ${
              totalChunks === 1 ? 'chunk' : 'chunks'
            }…`
          : 'Writing your answer…',
      detail: latestToolQuery ? `Searched for: "${latestToolQuery}"` : '',
    };
  }

  if (latestToolQuery) {
    return {
      stage: 'Searching your documents…',
      detail: `Looking for: "${latestToolQuery}"`,
    };
  }

  return {
    stage:
      turnNumber > 1
        ? 'Reviewing conversation context…'
        : 'Understanding your question…',
    detail: '',
  };
}

function parseTurnSteps(turn, turnNumber) {
  const events = Array.isArray(turn?.events) ? turn.events : [];
  const searchRounds = [];
  let pendingQueries = [];

  for (const evt of events) {
    const t = (evt?.type || '').toLowerCase();
    if ((t === 'ai' || t === 'assistant') && Array.isArray(evt.tool_calls) && evt.tool_calls.length > 0) {
      const queries = evt.tool_calls
        .map((c) => c?.args?.query)
        .filter(Boolean);
      pendingQueries = queries.length > 0 ? queries : ['Document lookup'];
    } else if (t === 'tool' || t === 'function') {
      const chunks = parseRetrievedChunksXml(evt.content);
      searchRounds.push({
        queries: pendingQueries.length > 0 ? [...pendingQueries] : [],
        chunks,
        rawContent: evt.content || '',
      });
      pendingQueries = [];
    }
  }

  // If a search was dispatched and we are still awaiting the tool response
  if (pendingQueries.length > 0) {
    searchRounds.push({
      queries: [...pendingQueries],
      chunks: null, // pending
      rawContent: '',
    });
  }

  return {
    turnNumber,
    searchRounds,
    usedEarlierContextOnly:
      searchRounds.length === 0 &&
      turnNumber > 1 &&
      (turn?.status === 'done' || Boolean(turn?.answer)),
  };
}

const RAI_CATEGORY_LABELS = {
  HATE_SPEECH: 'Hate Speech',
  HARASSMENT: 'Harassment',
  SEXUALLY_EXPLICIT: 'Sexually Explicit',
  DANGEROUS: 'Dangerous Content',
  DANGEROUS_CONTENT: 'Dangerous Content',
};

function formatRaiCategory(code) {
  const key = String(code || '').trim().toUpperCase();
  if (!key) return '';
  if (RAI_CATEGORY_LABELS[key]) return RAI_CATEGORY_LABELS[key];
  return key
    .toLowerCase()
    .split('_')
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

function summarizeGuardrailPills(compliance) {
  if (!compliance) return [];
  const rawActions = Array.isArray(compliance.guardrail_actions)
    ? compliance.guardrail_actions
    : [];

  const grouped = new Map();
  for (const item of rawActions) {
    if (!item || !item.filter || !item.action) continue;
    const key = `${item.filter}:${item.action}`;
    if (!grouped.has(key)) {
      grouped.set(key, {
        filter: item.filter,
        label: item.label || item.filter,
        action: item.action,
        stages: item.stage ? [item.stage] : [],
        details: Array.isArray(item.details) ? [...item.details] : [],
      });
    } else {
      const existing = grouped.get(key);
      if (item.stage && !existing.stages.includes(item.stage)) {
        existing.stages.push(item.stage);
      }
      if (Array.isArray(item.details)) {
        for (const d of item.details) {
          if (d && !existing.details.includes(d)) existing.details.push(d);
        }
      }
    }
  }

  // Backward compatibility if an older cached turn only stored boolean pii_filtered
  if (grouped.size === 0 && compliance.pii_filtered) {
    grouped.set('sdp:redacted', {
      filter: 'sdp',
      label: 'PII / Sensitive Data',
      action: 'redacted',
      stages: ['output'],
      details: [],
    });
  }

  const pills = [];
  for (const [key, entry] of grouped.entries()) {
    const stageNote = entry.stages.length > 0 ? ` (${entry.stages.join(', ')})` : '';
    if (entry.action === 'redacted') {
      pills.push({
        key,
        variant: 'pii-redacted',
        text: 'PII redacted',
        title: `Cloud Model Armor SDP + Cloud DLP masked sensitive data${stageNote}`,
      });
      continue;
    }

    let text = `${entry.label} blocked`;
    if (entry.filter === 'pi_and_jailbreak') {
      text = 'Prompt Injection / Jailbreak blocked';
    } else if (entry.filter === 'malicious_uri') {
      text = 'Malicious URI blocked';
    } else if (entry.filter === 'rai') {
      const raiDetails = entry.details.map(formatRaiCategory).filter(Boolean);
      text =
        raiDetails.length > 0
          ? `RAI Safety blocked: ${raiDetails.join(', ')}`
          : 'RAI Safety blocked';
    } else if (entry.filter === 'sdp') {
      text = 'Sensitive Data blocked';
    } else if (entry.filter === 'csam') {
      text = 'CSAM Safety blocked';
    } else if (entry.filter === 'gemini_safety') {
      text = 'Gemini Safety blocked';
    } else if (entry.filter === 'invalid_query_text') {
      text = 'Query validation blocked';
    }

    const provider =
      entry.filter === 'gemini_safety'
        ? 'Vertex AI Gemini safety settings'
        : entry.filter === 'invalid_query_text'
          ? 'Input validation policy'
          : 'Google Cloud Model Armor';

    const detailSuffix =
      entry.details.length > 0 ? ` · Subcategories: ${entry.details.join(', ')}` : '';

    pills.push({
      key,
      variant: 'guardrail-blocked',
      text,
      title: `Blocked by ${provider}${stageNote}${detailSuffix}`,
    });
  }

  return pills;
}

function AgentComplianceFooter({ turn, groundingEvents = [] }) {
  if (!turn?.answer) return null;
  const compliance = turn.compliance || null;
  if (!compliance && turn.status === 'streaming') return null;

  const guardrailPills = summarizeGuardrailPills(compliance);
  const wasBlocked =
    compliance?.ai_generated === false ||
    guardrailPills.some((p) => p.variant === 'guardrail-blocked');

  let isGrounded = false;
  let citationCount = 0;
  if (compliance && typeof compliance.grounded === 'boolean') {
    isGrounded = compliance.grounded;
    citationCount = Number(compliance.citation_count || 0);
  } else {
    const fallbackCitations = collectAllCitations(turn.answer);
    const fallbackChunks = extractRetrievedChunksFromEvents(groundingEvents);
    citationCount = fallbackCitations.reduce(
      (sum, item) => sum + Math.max(Array.isArray(item.pages) ? item.pages.length : 0, 1),
      0
    );
    isGrounded = !wasBlocked && citationCount > 0 && fallbackChunks.length > 0;
  }

  const groundingTitle = isGrounded
    ? 'Answer is grounded in retrieved document chunks with inline page citations'
    : wasBlocked
      ? 'Blocked by safety/security guardrails before grounded output'
      : 'Response does not contain inline citations to retrieved document chunks';

  return (
    <div
      className="agent-compliance-footer"
      title={
        compliance?.request_id
          ? `EU AI Act Art. 50 Transparency · Request ID: ${compliance.request_id}`
          : 'EU AI Act Art. 50 Transparency'
      }
    >
      {compliance && (
        <span className="agent-compliance-pill">
          <Shield size={12} />
          <span>{wasBlocked ? 'Guardrail refusal' : 'AI-generated'}</span>
          {compliance.model && (
            <>
              <span className="agent-compliance-sep">·</span>
              <span>{compliance.model}</span>
            </>
          )}
          {compliance.region && (
            <>
              <span className="agent-compliance-sep">·</span>
              <span>{compliance.region}</span>
            </>
          )}
        </span>
      )}

      {isGrounded ? (
        <span className="agent-compliance-pill grounded" title={groundingTitle}>
          <CheckCircle2 size={12} />
          <span>
            Grounded
            {citationCount > 0
              ? ` · ${citationCount} ${citationCount === 1 ? 'citation' : 'citations'}`
              : ''}
          </span>
        </span>
      ) : (
        <span className="agent-compliance-pill ungrounded" title={groundingTitle}>
          <AlertCircle size={12} />
          <span>Not grounded</span>
        </span>
      )}

      {guardrailPills.map((pill) => (
        <span
          key={pill.key}
          className={`agent-compliance-pill ${pill.variant}`}
          title={pill.title}
        >
          {pill.variant === 'guardrail-blocked' ? (
            <AlertTriangle size={12} />
          ) : (
            <Shield size={12} />
          )}
          <span>{pill.text}</span>
        </span>
      ))}

      {compliance?.model_armor_active && guardrailPills.length === 0 && (
        <span
          className="agent-compliance-pill armor-verified"
          title="Passed Google Cloud Model Armor checks: Prompt Injection/Jailbreak, Malicious URI, RAI Safety, and PII/SDP"
        >
          <Shield size={12} />
          <span>Model Armor verified</span>
        </span>
      )}
    </div>
  );
}

function TurnStepsTimelineCard({
  turn,
  turnIndex,
  allPriorEvents,
  defaultExpandChunks = true,
  onBackToChat,
}) {
  const [expandedRounds, setExpandedRounds] = useState({});
  const [showAnswerPreview, setShowAnswerPreview] = useState(false);

  const turnNumber = turnIndex + 1;
  const turnEvents = Array.isArray(turn.events) ? turn.events : [];
  const groundingEvents = [...turnEvents, ...(allPriorEvents || [])];
  const { searchRounds, usedEarlierContextOnly } = parseTurnSteps(turn, turnNumber);
  const citations = turn.answer ? collectAllCitations(turn.answer) : [];
  const liveStep =
    turn.status === 'streaming' && !turn.answer
      ? describeLiveAgentStep(turnEvents, turn.endpointMode, turnNumber)
      : null;

  const isRoundExpanded = (rIdx) =>
    expandedRounds[rIdx] !== undefined ? expandedRounds[rIdx] : defaultExpandChunks;

  const toggleRound = (rIdx) => {
    setExpandedRounds((prev) => ({
      ...prev,
      [rIdx]: !isRoundExpanded(rIdx),
    }));
  };

  return (
    <div className="steps-turn-card">
      <div className="steps-turn-card-header">
        <div className="steps-turn-card-title">
          <span className="steps-turn-badge">Question #{turnNumber}</span>
          {turnNumber > 1 && (
            <span className="steps-turn-subbadge">Follow-up</span>
          )}
          {turn.createdAt && (
            <span className="chat-turn-time">{formatTurnTime(turn.createdAt)}</span>
          )}
        </div>
        {onBackToChat && (
          <button
            type="button"
            className="steps-jump-chat-link"
            onClick={onBackToChat}
          >
            <span>View in Chat</span>
          </button>
        )}
      </div>

      <div className="steps-timeline">
        {/* Step 1: Question */}
        <div className="step-node">
          <div className="step-node-indicator question">
            <MessageSquare size={14} />
          </div>
          <div className="step-node-body">
            <div className="step-node-label">Your Question</div>
            <div className="step-question-box">{turn.query}</div>
          </div>
        </div>

        {/* Step 2..N: Search Rounds & Matched Chunks */}
        {searchRounds.map((round, rIdx) => {
          const primaryQuery = round.queries[0] || turn.query || '';
          const highlightTerms = extractQueryTerms(primaryQuery);
          const expanded = isRoundExpanded(rIdx);
          const chunkCount = Array.isArray(round.chunks) ? round.chunks.length : 0;

          return (
            <React.Fragment key={`round-${rIdx}`}>
              {round.queries.length > 0 && (
                <div className="step-node">
                  <div className="step-node-indicator search">
                    <Search size={14} />
                  </div>
                  <div className="step-node-body">
                    <div className="step-node-label">
                      {searchRounds.length > 1
                        ? `Document Search #${rIdx + 1}`
                        : 'Searched Documents'}
                    </div>
                    <div className="step-queries-list">
                      {round.queries.map((q, qIdx) => (
                        <div key={qIdx} className="step-query-pill">
                          <Search size={12} className="step-query-pill-icon" />
                          <span>"{q}"</span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {round.chunks !== null && (
                <div className="step-node">
                  <div className="step-node-indicator chunks">
                    <BookOpen size={14} />
                  </div>
                  <div className="step-node-body">
                    <div className="step-chunks-header-row">
                      <div className="step-node-label" style={{ margin: 0 }}>
                        {chunkCount > 0
                          ? `Matched ${chunkCount} ${
                              chunkCount === 1 ? 'Chunk' : 'Chunks'
                            }`
                          : 'No Matching Chunks Found'}
                      </div>
                      {chunkCount > 0 && (
                        <button
                          type="button"
                          className="step-collapse-btn"
                          onClick={() => toggleRound(rIdx)}
                        >
                          {expanded ? (
                            <>
                              <ChevronUp size={13} />
                              <span>Hide chunks</span>
                            </>
                          ) : (
                            <>
                              <ChevronDown size={13} />
                              <span>Show {chunkCount} chunks</span>
                            </>
                          )}
                        </button>
                      )}
                    </div>

                    {chunkCount > 0 && expanded && (
                      <div className="step-chunks-grid">
                        {round.chunks.map((chunk, cIdx) => {
                          const wordCount = (chunk.content || '').trim()
                            ? chunk.content.trim().split(/\s+/).length
                            : 0;
                          return (
                            <div key={cIdx} className="step-chunk-card">
                              <div className="step-chunk-card-header">
                                <div className="step-chunk-card-left">
                                  <span className="step-chunk-index">
                                    Chunk #{cIdx + 1}
                                  </span>
                                  <span className="result-stats-pill">
                                    {wordCount} words
                                  </span>
                                </div>
                                <span className="result-source-tag">
                                  <FileText size={12} className="result-source-icon" />
                                  <span className="result-source-name">{chunk.source}</span>
                                  <span className="result-page-badge">p. {chunk.page}</span>
                                </span>
                              </div>
                              <FormattedChunkView
                                rawText={chunk.content}
                                queryTerms={highlightTerms}
                                viewMode="reflowed"
                                highlightEnabled={highlightTerms.length > 0}
                              />
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </React.Fragment>
          );
        })}

        {usedEarlierContextOnly && (
          <div className="step-node">
            <div className="step-node-indicator context">
              <History size={14} />
            </div>
            <div className="step-node-body">
              <div className="step-node-label">Used Conversation Context</div>
              <div className="step-context-note">
                No new document search was needed — answered using context from earlier in this chat.
              </div>
            </div>
          </div>
        )}

        {/* Final Step: Live Status / Error / Answer Summary */}
        <div className="step-node last">
          <div
            className={`step-node-indicator ${
              turn.status === 'error' && !turn.answer ? 'error' : 'answer'
            }`}
          >
            {liveStep ? (
              <RefreshCw size={14} className="spinner" />
            ) : turn.status === 'error' && !turn.answer ? (
              <AlertCircle size={14} />
            ) : (
              <Sparkles size={14} />
            )}
          </div>
          <div className="step-node-body">
            {liveStep ? (
              <>
                <div className="step-node-label">In Progress</div>
                <div className="step-live-box">
                  <div className="agent-live-step-stage">{liveStep.stage}</div>
                  {liveStep.detail && (
                    <div className="agent-live-step-detail">{liveStep.detail}</div>
                  )}
                </div>
              </>
            ) : turn.status === 'error' && !turn.answer ? (
              <>
                <div className="step-node-label">Stopped / Error</div>
                <div className="agent-turn-error-box">
                  <span>{turn.error || 'Could not generate an answer.'}</span>
                </div>
              </>
            ) : (
              <>
                <div className="step-chunks-header-row">
                  <div className="step-node-label" style={{ margin: 0 }}>
                    Answer Ready
                  </div>
                  {turn.answer && (
                    <button
                      type="button"
                      className="step-collapse-btn"
                      onClick={() => setShowAnswerPreview((prev) => !prev)}
                    >
                      {showAnswerPreview ? (
                        <>
                          <ChevronUp size={13} />
                          <span>Hide answer preview</span>
                        </>
                      ) : (
                        <>
                          <ChevronDown size={13} />
                          <span>Preview answer</span>
                        </>
                      )}
                    </button>
                  )}
                </div>

                <div className="step-answer-summary-card">
                  {citations.length > 0 ? (
                    <div className="step-answer-citations-row">
                      <span className="step-answer-cited-label">Pages cited:</span>
                      {citations.map((srcItem, sIdx) => (
                        <span key={sIdx} className="result-source-tag">
                          <FileText size={12} className="result-source-icon" />
                          <span className="result-source-name">{srcItem.source}</span>
                          {srcItem.pages.length > 0 && (
                            <span className="result-page-badge">
                              p. {srcItem.pages.join(', ')}
                            </span>
                          )}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <span className="step-answer-plain-note">
                      Answer completed — switch to Chat to read or ask a follow-up.
                    </span>
                  )}

                  {showAnswerPreview && turn.answer && (
                    <div className="step-answer-preview-body">
                      <AgentMarkdownView
                        markdown={turn.answer}
                        events={groundingEvents}
                        compact
                      />
                    </div>
                  )}

                  {turn.answer && (
                    <AgentComplianceFooter
                      turn={turn}
                      groundingEvents={groundingEvents}
                    />
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function AgentSearchStepsView({
  turns = [],
  focusedTurnId = 'latest',
  onSelectTurnFilter,
  onBackToChat,
}) {
  const [formatMode, setFormatMode] = useState('visual'); // 'visual' | 'json'
  const [copied, setCopied] = useState(false);

  if (!Array.isArray(turns) || turns.length === 0) {
    return (
      <div className="agent-empty-thread">
        <div className="agent-empty-icon-wrap">
          <Search size={22} />
        </div>
        <h3 className="agent-empty-title">No Search Steps Yet</h3>
        <p className="agent-empty-desc">
          Ask a question in <strong>Chat</strong> first — then toggle here anytime to see
          the exact search phrases and document chunks used to write each answer.
        </p>
        <button
          type="button"
          className="btn-new-thread"
          style={{ marginTop: '0.4rem' }}
          onClick={onBackToChat}
        >
          <ArrowLeft size={14} />
          <span>Back to Chat</span>
        </button>
      </div>
    );
  }

  const latestTurn = turns[turns.length - 1];
  const resolvedFilter =
    focusedTurnId === 'all' && turns.length > 1
      ? 'all'
      : turns.some((t) => t.id === focusedTurnId)
      ? focusedTurnId
      : latestTurn.id;

  const displayedItems =
    resolvedFilter === 'all'
      ? turns.map((turn, idx) => ({ turn, idx }))
      : turns
          .map((turn, idx) => ({ turn, idx }))
          .filter((item) => item.turn.id === resolvedFilter);

  const jsonPayload =
    resolvedFilter === 'all'
      ? turns.map((t, i) => ({
          question_number: i + 1,
          question: t.query,
          steps: t.events || [],
        }))
      : displayedItems[0]?.turn?.events || [];

  const handleCopyJson = () => {
    navigator.clipboard.writeText(JSON.stringify(jsonPayload, null, 2));
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  };

  return (
    <div className="agent-steps-view">
      {/* Top Toolbar: Question Switcher + Visual/JSON + Back to Chat */}
      <div className="agent-steps-toolbar">
        <div className="agent-steps-filter-group">
          {turns.length > 1 ? (
            <div className="agent-steps-turn-pills" role="tablist" aria-label="Select question">
              {turns.map((t, idx) => {
                const isSelected = resolvedFilter === t.id;
                const shortQ =
                  t.query.length > 30 ? `${t.query.slice(0, 29).trimEnd()}…` : t.query;
                return (
                  <button
                    key={t.id}
                    type="button"
                    role="tab"
                    aria-selected={isSelected}
                    className={`steps-filter-pill ${isSelected ? 'active' : ''}`}
                    onClick={() => onSelectTurnFilter(t.id)}
                    title={t.query}
                  >
                    <span className="steps-filter-pill-num">Q{idx + 1}</span>
                    <span className="steps-filter-pill-text">{shortQ}</span>
                  </button>
                );
              })}
              <button
                type="button"
                role="tab"
                aria-selected={resolvedFilter === 'all'}
                className={`steps-filter-pill ${resolvedFilter === 'all' ? 'active' : ''}`}
                onClick={() => onSelectTurnFilter('all')}
              >
                <Layers size={12} />
                <span>All ({turns.length})</span>
              </button>
            </div>
          ) : (
            <div className="steps-single-heading">
              <Search size={15} color="var(--accent-cyan)" />
              <span>How the assistant searched your documents</span>
            </div>
          )}
        </div>

        <div className="agent-steps-toolbar-right">
          <div className="chunk-view-segmented" role="group" aria-label="Steps format">
            <button
              type="button"
              className={`chunk-view-btn ${formatMode === 'visual' ? 'active' : ''}`}
              onClick={() => setFormatMode('visual')}
              title="Clean step-by-step visual timeline"
            >
              <Sparkles size={12} />
              <span>Visual</span>
            </button>
            <button
              type="button"
              className={`chunk-view-btn ${formatMode === 'json' ? 'active' : ''}`}
              onClick={() => setFormatMode('json')}
              title="Inspect raw JSON data"
            >
              <Code2 size={12} />
              <span>JSON</span>
            </button>
          </div>

          {formatMode === 'json' && (
            <button
              type="button"
              className={`chunk-copy-btn ${copied ? 'copied' : ''}`}
              onClick={handleCopyJson}
              title="Copy JSON"
            >
              {copied ? <Check size={13} /> : <Copy size={13} />}
              <span>{copied ? 'Copied' : 'Copy'}</span>
            </button>
          )}

          <button
            type="button"
            className="steps-back-btn"
            onClick={onBackToChat}
            title="Return to conversation"
          >
            <ArrowLeft size={13} />
            <span>Back to Chat</span>
          </button>
        </div>
      </div>

      {/* Body: Visual Step Timeline or Raw JSON */}
      {formatMode === 'json' ? (
        <div className="steps-json-panel">
          <pre>{JSON.stringify(jsonPayload, null, 2)}</pre>
        </div>
      ) : (
        <div className="steps-cards-list">
          {displayedItems.map(({ turn, idx }) => {
            const priorEventsReversed = turns
              .slice(0, idx)
              .reverse()
              .flatMap((t) => (Array.isArray(t.events) ? t.events : []));
            return (
              <TurnStepsTimelineCard
                key={turn.id}
                turn={turn}
                turnIndex={idx}
                allPriorEvents={priorEventsReversed}
                defaultExpandChunks={
                  resolvedFilter !== 'all' || idx === turns.length - 1
                }
                onBackToChat={onBackToChat}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

function ConversationTurnCard({
  turn,
  turnIndex,
  priorTurns,
  onRetryTurn,
  onStopTurn,
  onOpenSteps,
  agentLoading,
}) {
  const [viewMode, setViewMode] = useState('rendered'); // 'rendered' | 'raw'
  const [copied, setCopied] = useState(false);

  const turnEvents = Array.isArray(turn.events) ? turn.events : [];
  // Prioritize chunks retrieved in the current turn, falling back to earlier turns in this conversation
  const priorEventsReversed = [...priorTurns]
    .reverse()
    .flatMap((t) => (Array.isArray(t.events) ? t.events : []));
  const groundingEvents = [...turnEvents, ...priorEventsReversed];

  const retrievalStats = summarizeTurnRetrieval(turnEvents);
  const liveStep =
    turn.status === 'streaming' && !turn.answer
      ? describeLiveAgentStep(turnEvents, turn.endpointMode, turnIndex + 1)
      : null;

  const handleCopy = () => {
    if (!turn.answer) return;
    navigator.clipboard.writeText(turn.answer);
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  };

  return (
    <div className="chat-turn-block">
      {/* User Prompt Row */}
      <div className="chat-user-row">
        <div className="chat-user-meta">
          <span className="chat-role-label">You</span>
          {turn.createdAt && (
            <span className="chat-turn-time">{formatTurnTime(turn.createdAt)}</span>
          )}
        </div>
        <div className="chat-user-bubble">{turn.query}</div>
      </div>

      {/* Grounded Assistant Response Card */}
      <div className="agent-response-card chat-agent-card">
        <div className="agent-response-header">
          <div className="agent-response-title-group">
            <div className="agent-response-title">
              <Sparkles size={18} color="var(--primary)" />
              <span>Assistant</span>
            </div>
            {retrievalStats.chunksCount > 0 ? (
              <button
                type="button"
                className="agent-grounding-badge clickable"
                onClick={() => onOpenSteps && onOpenSteps(turn.id)}
                title="Click to inspect the search steps and matched chunks"
              >
                <span>
                  {retrievalStats.chunksCount}{' '}
                  {retrievalStats.chunksCount === 1 ? 'chunk' : 'chunks'} used
                </span>
              </button>
            ) : retrievalStats.toolCallsCount > 0 && turn.status === 'done' ? (
              <span className="agent-grounding-badge muted">No matching chunks</span>
            ) : turnIndex > 0 && turn.status === 'done' ? (
              <span
                className="agent-grounding-badge memory"
                title="Answered using context from earlier in this conversation"
              >
                <History size={11} />
                <span>From earlier in chat</span>
              </span>
            ) : null}
          </div>

          <div className="agent-response-actions">
            {turn.answer && (
              <>
                <div className="chunk-view-segmented" role="group" aria-label="Answer display mode">
                  <button
                    type="button"
                    className={`chunk-view-btn ${viewMode === 'rendered' ? 'active' : ''}`}
                    onClick={() => setViewMode('rendered')}
                    title="Formatted answer with clickable page citations"
                  >
                    <AlignLeft size={13} />
                    <span>Formatted</span>
                  </button>
                  <button
                    type="button"
                    className={`chunk-view-btn ${viewMode === 'raw' ? 'active' : ''}`}
                    onClick={() => setViewMode('raw')}
                    title="Plain text view"
                  >
                    <FileText size={13} />
                    <span>Plain Text</span>
                  </button>
                </div>

                <button
                  type="button"
                  className={`chunk-copy-btn ${copied ? 'copied' : ''}`}
                  onClick={handleCopy}
                  title={copied ? 'Copied!' : 'Copy answer'}
                >
                  {copied ? <Check size={13} /> : <Copy size={13} />}
                  <span>{copied ? 'Copied' : 'Copy'}</span>
                </button>
              </>
            )}

            {turnEvents.length > 0 && onOpenSteps && (
              <button
                type="button"
                className="btn-event-stream"
                onClick={() => onOpenSteps(turn.id)}
                title="Open Search Steps view for this answer"
              >
                <Search size={13} />
                <span>Search Steps</span>
              </button>
            )}
          </div>
        </div>

        {liveStep ? (
          <div className="agent-live-step-card">
            <div className="agent-live-step-main">
              <RefreshCw className="spinner" size={20} style={{ color: 'var(--primary)', flexShrink: 0 }} />
              <div className="agent-live-step-text">
                <div className="agent-live-step-stage">{liveStep.stage}</div>
                {liveStep.detail && (
                  <div className="agent-live-step-detail">{liveStep.detail}</div>
                )}
              </div>
            </div>
            {onStopTurn && (
              <button
                type="button"
                className="agent-stop-inline-btn"
                onClick={onStopTurn}
                title="Stop generating answer"
              >
                <Square size={11} />
                <span>Stop</span>
              </button>
            )}
          </div>
        ) : turn.status === 'error' && !turn.answer ? (
          <div className="agent-turn-error-box">
            <div className="agent-turn-error-msg">
              <AlertCircle size={17} style={{ flexShrink: 0 }} />
              <span>{turn.error || 'Could not generate an answer. Please try again.'}</span>
            </div>
            {onRetryTurn && (
              <button
                type="button"
                className="agent-retry-btn"
                onClick={() => onRetryTurn(turn)}
                disabled={agentLoading}
              >
                <RotateCcw size={13} />
                <span>Try Again</span>
              </button>
            )}
          </div>
        ) : (
          <>
            <AgentMarkdownView
              markdown={turn.answer}
              events={groundingEvents}
              viewMode={viewMode}
              queryTerms={extractQueryTerms(turn.query)}
            />
            {turn.answer && (
              <AgentComplianceFooter
                turn={turn}
                groundingEvents={groundingEvents}
              />
            )}
            {turn.status === 'error' && turn.error && (
              <div className="agent-turn-error-box" style={{ marginTop: '0.85rem' }}>
                <div className="agent-turn-error-msg">
                  <AlertCircle size={16} style={{ flexShrink: 0 }} />
                  <span>{turn.error}</span>
                </div>
                {onRetryTurn && (
                  <button
                    type="button"
                    className="agent-retry-btn"
                    onClick={() => onRetryTurn(turn)}
                    disabled={agentLoading}
                  >
                    <RotateCcw size={13} />
                    <span>Try Again</span>
                  </button>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function PrivacyDataModal({
  isOpen,
  onClose,
  inventory,
  loading,
  erasing,
  error,
  confirmErase,
  setConfirmErase,
  onRefresh,
  onConfirmErase,
}) {
  useEffect(() => {
    if (!isOpen) return undefined;
    const onKeyDown = (e) => {
      if (e.key === 'Escape' && !erasing) {
        onClose();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [isOpen, erasing, onClose]);

  if (!isOpen) return null;

  const collections = Array.isArray(inventory?.collections) ? inventory.collections : [];
  const uploadedPdfs = Array.isArray(inventory?.uploaded_pdfs) ? inventory.uploaded_pdfs : [];

  return (
    <div
      className="privacy-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="privacy-modal-title"
      onClick={() => {
        if (!erasing) onClose();
      }}
    >
      <div className="privacy-modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="privacy-modal-header">
          <div className="privacy-modal-title-wrap">
            <div className="privacy-modal-icon">
              <Shield size={20} />
            </div>
            <div>
              <h2 id="privacy-modal-title" className="privacy-modal-title">
                My Data &amp; Privacy
              </h2>
              <p className="privacy-modal-subtitle">
                GDPR Art. 15 Personal Data Inventory &amp; Art. 17 Right to Erasure
              </p>
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
            <button
              type="button"
              className="refresh-btn"
              onClick={onRefresh}
              disabled={loading || erasing}
              title="Refresh data inventory"
            >
              <RefreshCw size={15} className={loading ? 'spinner' : ''} />
            </button>
            <button
              type="button"
              className="refresh-btn"
              onClick={onClose}
              disabled={erasing}
              aria-label="Close My Data & Privacy"
            >
              <X size={17} />
            </button>
          </div>
        </div>

        <div className="privacy-modal-body">
          {error && (
            <div className="alert alert-error" style={{ marginBottom: '1rem' }}>
              <AlertCircle size={18} style={{ flexShrink: 0 }} />
              <span>{error}</span>
            </div>
          )}

          {loading && !inventory ? (
            <div className="privacy-loading-box">
              <RefreshCw size={18} className="spinner" color="var(--primary)" />
              <span>Loading your stored data inventory…</span>
            </div>
          ) : inventory ? (
            <>
              <div className="privacy-stats-grid">
                <div className="privacy-stat-tile">
                  <span className="privacy-stat-label">Data Region</span>
                  <span className="privacy-stat-value">{inventory.region || '—'}</span>
                </div>
                <div className="privacy-stat-tile">
                  <span className="privacy-stat-label">Collections</span>
                  <span className="privacy-stat-value">
                    {inventory.collection_count ?? 0}{' '}
                    <span className="privacy-stat-sub">
                      ({inventory.total_chunks ?? 0} chunks)
                    </span>
                  </span>
                </div>
                <div className="privacy-stat-tile">
                  <span className="privacy-stat-label">Archived PDFs</span>
                  <span className="privacy-stat-value">{inventory.uploaded_pdf_count ?? 0}</span>
                </div>
                <div className="privacy-stat-tile">
                  <span className="privacy-stat-label">Stored Volume</span>
                  <span className="privacy-stat-value">
                    {formatStorageBytes(inventory.total_bytes)}
                  </span>
                </div>
              </div>

              <div className="privacy-meta-strip">
                <span>
                  Storage: <strong>{inventory.storage_backend === 'gcs' ? 'Regional Cloud Storage' : 'Local Disk'}</strong>
                </span>
                {inventory.storage_backend === 'gcs' && (
                  <span>
                    GCS Objects: <strong>{inventory.gcs_live_object_count ?? 0} live</strong> ·{' '}
                    <strong>{inventory.gcs_noncurrent_version_count ?? 0} versioned</strong>
                  </span>
                )}
                <span>
                  Active Chat Memory: <strong>{inventory.memory_thread_count ?? 0} thread(s)</strong>
                </span>
              </div>

              <div className="privacy-section">
                <h3 className="privacy-section-title">Indexed Vector Collections</h3>
                {collections.length === 0 ? (
                  <p className="privacy-empty-note">No vector collections stored.</p>
                ) : (
                  <div className="privacy-list">
                    {collections.map((col) => (
                      <div key={col.name} className="privacy-list-row">
                        <span className="privacy-list-name">
                          <Layers size={14} color="var(--primary)" />
                          <span>{col.name}</span>
                        </span>
                        <span className="collection-badge">
                          {col.count ?? 0} {col.count === 1 ? 'chunk' : 'chunks'}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {inventory.storage_backend === 'gcs' && (
                <div className="privacy-section">
                  <h3 className="privacy-section-title">Archived Uploaded PDFs</h3>
                  {uploadedPdfs.length === 0 ? (
                    <p className="privacy-empty-note">No archived PDF uploads stored.</p>
                  ) : (
                    <div className="privacy-list">
                      {uploadedPdfs.map((pdf) => (
                        <div key={pdf.blob_name} className="privacy-list-row">
                          <span className="privacy-list-name" title={pdf.blob_name}>
                            <FileText size={14} color="var(--accent-cyan)" />
                            <span>{pdf.filename}</span>
                          </span>
                          <span className="collection-badge">
                            {formatStorageBytes(pdf.size_bytes)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </>
          ) : null}

          <div className="privacy-danger-zone">
            <div className="privacy-danger-header">
              <div>
                <h3 className="privacy-danger-title">Erase All My Data (GDPR Art. 17)</h3>
                <p className="privacy-danger-desc">
                  Permanently deletes all vector collections, archived PDFs (including all live and
                  noncurrent Cloud Storage object versions), temporary caches, and active AI
                  conversation memory for your account.
                </p>
              </div>
              {!confirmErase && (
                <button
                  type="button"
                  className="privacy-erase-trigger-btn"
                  onClick={() => setConfirmErase(true)}
                  disabled={loading || erasing}
                >
                  <Trash2 size={14} />
                  <span>Erase All Data</span>
                </button>
              )}
            </div>

            {confirmErase && (
              <div className="privacy-confirm-box">
                <div className="privacy-confirm-warning">
                  <AlertTriangle size={17} style={{ flexShrink: 0 }} />
                  <span>
                    This action is irreversible. Permanently erase all stored collections, PDFs, and
                    conversation history?
                  </span>
                </div>
                <div className="privacy-confirm-actions">
                  <button
                    type="button"
                    className="tab-btn"
                    style={{ padding: '0.4rem 0.85rem', fontSize: '0.82rem' }}
                    onClick={() => setConfirmErase(false)}
                    disabled={erasing}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="privacy-erase-confirm-btn"
                    onClick={onConfirmErase}
                    disabled={erasing}
                  >
                    {erasing ? <RefreshCw size={14} className="spinner" /> : <Trash2 size={14} />}
                    <span>{erasing ? 'Erasing…' : 'Permanently Erase Everything'}</span>
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [currentUser, setCurrentUser] = useState(null);
  const [authInitializing, setAuthInitializing] = useState(true);

  const [activeTab, setActiveTab] = useState('ingest'); // 'ingest' | 'query' | 'agent' | 'manage'

  // Available & selected Vertex AI models (from window.__ENV__ / Vite .env / backend /collections)
  const [availableLlmModels, setAvailableLlmModels] = useState(DEFAULT_ALLOWED_LLM_MODELS);
  const [availableEmbeddingModels, setAvailableEmbeddingModels] = useState(
    DEFAULT_ALLOWED_EMBEDDING_MODELS
  );
  const [ingestEmbeddingModel, setIngestEmbeddingModel] = useState(
    DEFAULT_ALLOWED_EMBEDDING_MODELS[0] || 'gemini-embedding-001'
  );
  const [queryEmbeddingModel, setQueryEmbeddingModel] = useState(
    DEFAULT_ALLOWED_EMBEDDING_MODELS[0] || 'gemini-embedding-001'
  );
  const [agentLlmModel, setAgentLlmModel] = useState(
    DEFAULT_ALLOWED_LLM_MODELS[0] || 'gemini-2.5-flash'
  );
  const [agentEmbeddingModel, setAgentEmbeddingModel] = useState(
    DEFAULT_ALLOWED_EMBEDDING_MODELS[0] || 'gemini-embedding-001'
  );

  const [collections, setCollections] = useState([]);
  const [collectionDetails, setCollectionDetails] = useState([]);
  const [deletingCol, setDeletingCol] = useState(null);
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
  const [submittedQuery, setSubmittedQuery] = useState('');
  const [topK, setTopK] = useState(5);
  const [queryLoading, setQueryLoading] = useState(false);
  const [queryResults, setQueryResults] = useState(null);
  const [expandedMeta, setExpandedMeta] = useState({});
  const [chunkViewMode, setChunkViewMode] = useState('reflowed'); // 'reflowed' | 'raw'
  const [highlightMatches, setHighlightMatches] = useState(true);
  const [copiedChunkIdx, setCopiedChunkIdx] = useState(null);

  // Agent tab multi-turn state (backed by LangGraph InMemorySaver on backend)
  const initialThreadRef = useRef(null);
  if (!initialThreadRef.current) {
    initialThreadRef.current = createConversationThread('');
  }
  const [agentThreads, setAgentThreads] = useState([initialThreadRef.current]);
  const [activeThreadId, setActiveThreadId] = useState(initialThreadRef.current.id);
  const [agentCollection, setAgentCollection] = useState('');
  const [agentQuery, setAgentQuery] = useState('');
  const [agentEndpointMode, setAgentEndpointMode] = useState('stream'); // 'stream' | 'standard'
  const [agentViewMode, setAgentViewMode] = useState('chat'); // 'chat' | 'steps'
  const [focusedStepTurnId, setFocusedStepTurnId] = useState('latest');
  const [agentLoading, setAgentLoading] = useState(false);

  // Compliance & Privacy (GDPR Art. 15 / Art. 17) modal state
  const [privacyOpen, setPrivacyOpen] = useState(false);
  const [privacyInventory, setPrivacyInventory] = useState(null);
  const [privacyLoading, setPrivacyLoading] = useState(false);
  const [privacyErasing, setPrivacyErasing] = useState(false);
  const [privacyConfirmErase, setPrivacyConfirmErase] = useState(false);
  const [privacyError, setPrivacyError] = useState(null);

  const fileInputRef = useRef(null);
  const agentInputRef = useRef(null);
  const conversationEndRef = useRef(null);
  const abortControllerRef = useRef(null);
  const restoredUidRef = useRef(null);

  const activeThread =
    agentThreads.find((t) => t.id === activeThreadId) || agentThreads[0] || null;

  const getCollectionEmbeddingModel = (colName, detailsList = collectionDetails, embModels = availableEmbeddingModels) => {
    if (!colName || !Array.isArray(detailsList)) return null;
    const found = detailsList.find((d) => d && d.name === colName);
    if (found?.embedding_model && embModels.includes(found.embedding_model)) {
      return found.embedding_model;
    }
    return null;
  };

  const resetTenantState = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    restoredUidRef.current = null;
    const freshThread = createConversationThread('');
    setCollections([]);
    setCollectionDetails([]);
    setDeletingCol(null);
    setSelectedCollection('');
    setNewCollectionName('');
    setFiles([]);
    setPdfPaths('');
    setQueryCollection('');
    setQueryText('');
    setSubmittedQuery('');
    setQueryResults(null);
    setExpandedMeta({});
    setCopiedChunkIdx(null);
    setAgentThreads([freshThread]);
    setActiveThreadId(freshThread.id);
    setAgentCollection('');
    setAgentQuery('');
    setAgentViewMode('chat');
    setFocusedStepTurnId('latest');
    setAgentLoading(false);
    setPrivacyOpen(false);
    setPrivacyInventory(null);
    setPrivacyLoading(false);
    setPrivacyErasing(false);
    setPrivacyConfirmErase(false);
    setPrivacyError(null);
    setNotification(null);
  };

  useEffect(() => {
    const unsubscribe = subscribeToAuthChanges((firebaseUser) => {
      resetTenantState();
      setCurrentUser(firebaseUser);
      setAuthInitializing(false);
    });
    return () => unsubscribe();
  }, []);

  const handleSignOut = async () => {
    const uidToClear = currentUser?.uid;
    try {
      if (!currentUser?.isLocalDev) {
        await logoutUser();
      }
    } finally {
      if (uidToClear && typeof window !== 'undefined') {
        window.sessionStorage.removeItem(getThreadStorageKey(uidToClear));
      }
      resetTenantState();
      setCurrentUser(null);
    }
  };

  // Wrapper injecting Firebase ID Token (or X-Dev-User-Id in localhost dev mode)
  const authFetch = async (url, options = {}) => {
    const buildHeaders = async (forceRefresh = false) => {
      const headers = new Headers(options.headers || {});
      if (currentUser?.isLocalDev) {
        headers.set('X-Dev-User-Id', currentUser.uid);
      } else if (currentUser && typeof currentUser.getIdToken === 'function') {
        const token = await currentUser.getIdToken(forceRefresh);
        headers.set('Authorization', `Bearer ${token}`);
      }
      return headers;
    };

    let res = await fetch(url, {
      ...options,
      headers: await buildHeaders(false),
    });

    if (res.status === 401 && currentUser && typeof currentUser.getIdToken === 'function') {
      res = await fetch(url, {
        ...options,
        headers: await buildHeaders(true),
      });
    }

    return res;
  };

  // Fetch collections from backend (mounted via src.manage on agent-api)
  const fetchCollections = async (preferredAgentCol = null) => {
    if (!currentUser) return;
    try {
      setApiStatus('checking');
      const res = await authFetch(`${AGENT_API_BASE}/collections`);
      if (!res.ok) throw new Error('Failed to fetch collections');
      const data = await res.json();
      const colList = data.collections || [];
      const details = data.details || colList.map(c => ({ name: c, count: 0 }));

      let nextLlmModels = availableLlmModels;
      if (Array.isArray(data.allowed_llm_models) && data.allowed_llm_models.length > 0) {
        nextLlmModels = data.allowed_llm_models;
        setAvailableLlmModels(nextLlmModels);
        setAgentLlmModel((prev) =>
          nextLlmModels.includes(prev) ? prev : nextLlmModels[0]
        );
      }

      let nextEmbModels = availableEmbeddingModels;
      if (
        Array.isArray(data.allowed_embedding_models) &&
        data.allowed_embedding_models.length > 0
      ) {
        nextEmbModels = data.allowed_embedding_models;
        setAvailableEmbeddingModels(nextEmbModels);
        setIngestEmbeddingModel((prev) =>
          nextEmbModels.includes(prev) ? prev : nextEmbModels[0]
        );
        setQueryEmbeddingModel((prev) =>
          nextEmbModels.includes(prev) ? prev : nextEmbModels[0]
        );
        setAgentEmbeddingModel((prev) =>
          nextEmbModels.includes(prev) ? prev : nextEmbModels[0]
        );
      }

      setCollections(colList);
      setCollectionDetails(details);
      setApiStatus('online');
      
      if (colList.length > 0) {
        const nextIngestCol =
          !selectedCollection || !colList.includes(selectedCollection)
            ? colList[0]
            : selectedCollection;
        setSelectedCollection(nextIngestCol);
        const ingestColEmb = getCollectionEmbeddingModel(nextIngestCol, details, nextEmbModels);
        if (collectionMode === 'existing' && ingestColEmb) {
          setIngestEmbeddingModel(ingestColEmb);
        }

        const nextQueryCol =
          !queryCollection || !colList.includes(queryCollection)
            ? colList[0]
            : queryCollection;
        setQueryCollection(nextQueryCol);
        const queryColEmb = getCollectionEmbeddingModel(nextQueryCol, details, nextEmbModels);
        if (queryColEmb) {
          setQueryEmbeddingModel(queryColEmb);
        }

        const candidateAgentCol =
          typeof preferredAgentCol === 'string' && preferredAgentCol
            ? preferredAgentCol
            : agentCollection;
        const resolvedAgentCol =
          candidateAgentCol && colList.includes(candidateAgentCol)
            ? candidateAgentCol
            : colList[0];
        setAgentCollection(resolvedAgentCol);
        const agentColEmb = getCollectionEmbeddingModel(resolvedAgentCol, details, nextEmbModels);
        if (agentColEmb) {
          setAgentEmbeddingModel(agentColEmb);
        }
        setAgentThreads((prev) =>
          prev.map((t) =>
            t.turns.length === 0 && (!t.collection || !colList.includes(t.collection))
              ? { ...t, collection: resolvedAgentCol }
              : t
          )
        );
      } else {
        setSelectedCollection('');
        setQueryCollection('');
        setAgentCollection('');
      }
    } catch (err) {
      console.error('Error connecting to backend:', err);
      setApiStatus('offline');
      setNotification({
        type: 'error',
        message: 'Could not reach the document service. Please check your connection and try again.'
      });
    }
  };

  const handleDeleteCollection = async (colName) => {
    setNotification(null);
    setLoading(true);
    try {
      const res = await authFetch(`${AGENT_API_BASE}/collections/${encodeURIComponent(colName)}`, {
        method: 'DELETE',
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to delete collection');
      }
      setNotification({
        type: 'success',
        message: `Deleted collection '${colName}'.`
      });
      setDeletingCol(null);
      const remainingThreads = agentThreads.filter((t) => t.collection !== colName);
      if (remainingThreads.length === 0) {
        const fresh = createConversationThread('');
        setAgentThreads([fresh]);
        setActiveThreadId(fresh.id);
      } else if (remainingThreads.length !== agentThreads.length) {
        setAgentThreads(remainingThreads);
        if (!remainingThreads.some((t) => t.id === activeThreadId)) {
          setActiveThreadId(remainingThreads[0].id);
        }
      }
      await fetchCollections();
    } catch (err) {
      console.error('Delete collection error:', err);
      setNotification({
        type: 'error',
        message: err.message || `Failed to delete collection '${colName}'.`
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!currentUser?.uid) return;
    let restoredCol = null;
    const stored = loadStoredThreads(currentUser.uid);
    if (stored) {
      setAgentThreads(stored.threads);
      setActiveThreadId(stored.activeThreadId);
      const active =
        stored.threads.find((t) => t.id === stored.activeThreadId) ||
        stored.threads[0];
      if (active?.collection) {
        restoredCol = active.collection;
        setAgentCollection(active.collection);
      }
    }
    restoredUidRef.current = currentUser.uid;
    fetchCollections(restoredCol);
  }, [currentUser?.uid]);

  useEffect(() => {
    if (
      !currentUser?.uid ||
      restoredUidRef.current !== currentUser.uid ||
      typeof window === 'undefined'
    ) {
      return;
    }
    try {
      window.sessionStorage.setItem(
        getThreadStorageKey(currentUser.uid),
        JSON.stringify({
          threads: agentThreads,
          activeThreadId,
        })
      );
    } catch (e) {
      console.warn('Could not persist session threads:', e);
    }
  }, [currentUser?.uid, agentThreads, activeThreadId]);

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
        message: 'Please choose or enter a collection name.'
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
        message: 'Please enter at least one PDF file path.'
      });
      return;
    }

    setLoading(true);

    try {
      const formData = new FormData();
      formData.append('collection_name', targetCollection);
      if (ingestEmbeddingModel) {
        formData.append('embedding_model', ingestEmbeddingModel);
      }

      if (uploadMode === 'files') {
        files.forEach((file) => {
          formData.append('files', file);
        });
      } else {
        formData.append('pdf_paths', pdfPaths.trim());
      }

      const response = await authFetch(`${API_BASE}/ingest`, {
        method: 'POST',
        body: formData,
      });

      const result = await response.json();

      if (!response.ok) {
        throw new Error(result.detail || 'Failed to add documents');
      }

      setNotification({
        type: 'success',
        message: result.message || `Added documents to '${targetCollection}'.`,
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
    setExpandedMeta({});
    setCopiedChunkIdx(null);

    if (!queryCollection) {
      setNotification({
        type: 'error',
        message: 'Please choose a collection to search.'
      });
      return;
    }

    const trimmedQuery = queryText.trim();
    if (!trimmedQuery) {
      setNotification({
        type: 'error',
        message: 'Please enter something to search for.'
      });
      return;
    }

    setQueryLoading(true);

    try {
      const response = await authFetch(`${AGENT_API_BASE}/query`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          query: trimmedQuery,
          collection_name: queryCollection,
          k: Number(topK),
          embedding_model: queryEmbeddingModel,
        }),
      });

      const result = await response.json();

      if (!response.ok) {
        throw new Error(result.detail || 'Search failed');
      }

      setSubmittedQuery(trimmedQuery);
      setQueryResults(result.results || []);

    } catch (err) {
      console.error('Query error:', err);
      setNotification({
        type: 'error',
        message: err.message || 'An error occurred while searching your documents.'
      });
    } finally {
      setQueryLoading(false);
    }
  };

  const selectAgentCollection = (nextCol) => {
    setAgentCollection(nextCol);
    const colEmb = getCollectionEmbeddingModel(nextCol);
    if (colEmb) {
      setAgentEmbeddingModel(colEmb);
    }
    if (!nextCol) return;

    const current =
      agentThreads.find((t) => t.id === activeThreadId) || agentThreads[0];

    if (current && current.collection === nextCol) {
      if (activeThreadId !== current.id) {
        setActiveThreadId(current.id);
      }
      return;
    }

    setAgentViewMode('chat');
    setFocusedStepTurnId('latest');

    if (current && current.turns.length === 0) {
      setAgentThreads((prev) =>
        prev.map((t) =>
          t.id === current.id
            ? { ...t, collection: nextCol, updatedAt: Date.now() }
            : t
        )
      );
      if (activeThreadId !== current.id) {
        setActiveThreadId(current.id);
      }
      return;
    }

    const existingForCol = [...agentThreads]
      .filter((t) => t.collection === nextCol)
      .sort((a, b) => b.updatedAt - a.updatedAt)[0];

    if (existingForCol) {
      setActiveThreadId(existingForCol.id);
      return;
    }

    const fresh = createConversationThread(nextCol);
    setAgentThreads((prev) => [fresh, ...prev]);
    setActiveThreadId(fresh.id);
  };

  const handleNewThread = () => {
    if (agentLoading) return;
    const targetCol =
      activeThread?.collection || agentCollection || selectedCollection || '';

    if (activeThread && activeThread.turns.length === 0) {
      const rotatedId = createThreadId();
      setAgentThreads((prev) =>
        prev.map((t) =>
          t.id === activeThread.id
            ? {
                ...t,
                id: rotatedId,
                collection: targetCol,
                updatedAt: Date.now(),
              }
            : t
        )
      );
      setActiveThreadId(rotatedId);
    } else {
      const fresh = createConversationThread(targetCol);
      setAgentThreads((prev) => [fresh, ...prev]);
      setActiveThreadId(fresh.id);
    }

    setAgentViewMode('chat');
    setFocusedStepTurnId('latest');
    setAgentQuery('');
    setNotification(null);
    setTimeout(() => agentInputRef.current?.focus(), 30);
  };

  const handleSwitchThread = (thread) => {
    if (agentLoading || !thread) return;
    setActiveThreadId(thread.id);
    if (thread.collection && collections.includes(thread.collection)) {
      setAgentCollection(thread.collection);
      const colEmb = getCollectionEmbeddingModel(thread.collection);
      if (colEmb) {
        setAgentEmbeddingModel(colEmb);
      }
    }
    setAgentViewMode('chat');
    setFocusedStepTurnId('latest');
    setNotification(null);
  };

  const handleDeleteThread = (e, threadId) => {
    e.stopPropagation();
    if (agentLoading) return;
    const remaining = agentThreads.filter((t) => t.id !== threadId);
    if (remaining.length === 0) {
      const fallbackCol = agentCollection || selectedCollection || '';
      const fresh = createConversationThread(fallbackCol);
      setAgentThreads([fresh]);
      setActiveThreadId(fresh.id);
      setAgentViewMode('chat');
      setFocusedStepTurnId('latest');
    } else {
      setAgentThreads(remaining);
      if (activeThreadId === threadId) {
        const nextActive = [...remaining].sort((a, b) => b.updatedAt - a.updatedAt)[0];
        setActiveThreadId(nextActive.id);
        if (nextActive.collection && collections.includes(nextActive.collection)) {
          setAgentCollection(nextActive.collection);
        }
        setAgentViewMode('chat');
        setFocusedStepTurnId('latest');
      }
    }
  };

  const handleOpenTurnSteps = (turnId) => {
    setFocusedStepTurnId(turnId || 'latest');
    setAgentViewMode('steps');
  };

  const handleStopAgent = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
  };

  const updateTurnInThread = (tId, trnId, patchFn) => {
    setAgentThreads((prev) =>
      prev.map((t) => {
        if (t.id !== tId) return t;
        return {
          ...t,
          updatedAt: Date.now(),
          turns: t.turns.map((trn) => (trn.id === trnId ? patchFn(trn) : trn)),
        };
      })
    );
  };

  const submitAgentQuestion = async (rawQuestion, { retryTurnId = null } = {}) => {
    if (agentLoading) return;
    setNotification(null);

    const targetCollection =
      activeThread?.collection || agentCollection || selectedCollection;
    if (!targetCollection) {
      setNotification({
        type: 'error',
        message: 'Please choose a collection first.',
      });
      return;
    }

    const trimmedQuestion = (rawQuestion || '').trim();
    if (!trimmedQuestion) {
      setNotification({
        type: 'error',
        message: 'Please enter a question.',
      });
      return;
    }

    let targetThreadId = activeThread?.id;
    const turnId = `turn-${Date.now().toString(36)}-${Math.random()
      .toString(36)
      .slice(2, 6)}`;
    const now = Date.now();
    const newTurn = {
      id: turnId,
      query: trimmedQuestion,
      answer: '',
      events: [],
      compliance: null,
      endpointMode: agentEndpointMode,
      status: 'streaming',
      error: null,
      createdAt: now,
    };

    if (!targetThreadId) {
      const fresh = {
        ...createConversationThread(targetCollection),
        title: summarizeThreadTitle(trimmedQuestion),
        updatedAt: now,
        turns: [newTurn],
      };
      targetThreadId = fresh.id;
      setAgentThreads((prev) => [fresh, ...prev]);
      setActiveThreadId(fresh.id);
    } else {
      setAgentThreads((prev) =>
        prev.map((t) => {
          if (t.id !== targetThreadId) return t;
          const baseTurns = retryTurnId
            ? t.turns.filter((item) => item.id !== retryTurnId)
            : t.turns;
          const nextTitle =
            baseTurns.length === 0 ? summarizeThreadTitle(trimmedQuestion) : t.title;
          return {
            ...t,
            collection: t.collection || targetCollection,
            title: nextTitle,
            updatedAt: now,
            turns: [...baseTurns, newTurn],
          };
        })
      );
    }

    if (!retryTurnId) {
      setAgentQuery('');
    }
    setFocusedStepTurnId(turnId);
    setAgentLoading(true);

    setTimeout(() => {
      conversationEndRef.current?.scrollIntoView({
        behavior: 'smooth',
        block: 'end',
      });
    }, 50);

    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
      const endpointUrl =
        agentEndpointMode === 'standard'
          ? `${AGENT_API_BASE}/agent`
          : `${AGENT_API_BASE}/agent/stream`;
      const payload =
        agentEndpointMode === 'standard'
          ? {
              query: trimmedQuestion,
              collection_name: targetCollection,
              thread_id: targetThreadId,
              model_name: agentLlmModel,
              embedding_model: agentEmbeddingModel,
              stream: false,
            }
          : {
              query: trimmedQuestion,
              collection_name: targetCollection,
              thread_id: targetThreadId,
              model_name: agentLlmModel,
              embedding_model: agentEmbeddingModel,
            };

      const response = await authFetch(endpointUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });

      if (!response.ok) {
        const errJson = await response.json().catch(() => ({}));
        throw new Error(errJson.detail || `Server error: ${response.status}`);
      }

      const contentType = response.headers.get('content-type') || '';
      if (contentType.includes('text/event-stream')) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let streamDone = false;
        let latestAnswer = '';
        let latestCompliance = null;
        let streamError = null;
        const accumulatedEvents = [];

        const processLine = (rawLine) => {
          const trimmed = rawLine.trim();
          if (!trimmed.startsWith('data: ')) return false;

          const dataStr = trimmed.slice(6).trim();
          if (dataStr === '[DONE]') return true;

          try {
            const parsed = JSON.parse(dataStr);
            if (parsed.event === 'error' && parsed.error) {
              streamError = parsed.error;
            }
            if (parsed.event === 'compliance' && parsed.compliance) {
              latestCompliance = parsed.compliance;
              updateTurnInThread(targetThreadId, turnId, (trn) => ({
                ...trn,
                compliance: latestCompliance,
              }));
              return false;
            }
            if (parsed.message) {
              accumulatedEvents.push(parsed.message);
              const msgType = (parsed.message.type || '').toLowerCase();
              if (
                (msgType === 'ai' || msgType === 'assistant') &&
                parsed.message.content
              ) {
                latestAnswer = parsed.message.content;
              }
              updateTurnInThread(targetThreadId, turnId, (trn) => ({
                ...trn,
                events: [...accumulatedEvents],
                answer: latestAnswer,
                compliance: latestCompliance || trn.compliance || null,
              }));
            }
          } catch (e) {
            console.warn('Error parsing SSE json:', e);
          }
          return false;
        };

        while (!streamDone) {
          const { done, value } = await reader.read();
          if (done) {
            buffer += decoder.decode();
            if (buffer.trim()) {
              processLine(buffer);
            }
            break;
          }

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            if (processLine(line)) {
              streamDone = true;
              break;
            }
          }
        }

        updateTurnInThread(targetThreadId, turnId, (trn) => ({
          ...trn,
          events: [...accumulatedEvents],
          answer: latestAnswer,
          compliance: latestCompliance || trn.compliance || null,
          status: streamError && !latestAnswer ? 'error' : 'done',
          error: streamError && !latestAnswer ? streamError : null,
        }));
      } else {
        const data = await response.json();
        const msgs = Array.isArray(data.messages) ? data.messages : [];
        const ans = data.answer || '';
        updateTurnInThread(targetThreadId, turnId, (trn) => ({
          ...trn,
          events: msgs,
          answer: ans,
          compliance: data.compliance || null,
          status: 'done',
          error: null,
        }));
      }
    } catch (err) {
      if (err?.name === 'AbortError') {
        updateTurnInThread(targetThreadId, turnId, (trn) => ({
          ...trn,
          status: trn.answer ? 'done' : 'error',
          error: trn.answer ? null : 'Stopped.',
        }));
      } else {
        console.error('Agent execution error:', err);
        const errMessage =
          err.message || 'Could not get an answer right now. Please try again.';
        updateTurnInThread(targetThreadId, turnId, (trn) => ({
          ...trn,
          status: 'error',
          error: errMessage,
        }));
      }
    } finally {
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null;
      }
      setAgentLoading(false);
    }
  };

  const handleAgentSubmit = (e) => {
    e.preventDefault();
    submitAgentQuestion(agentQuery);
  };

  const handleRetryTurn = (failedTurn) => {
    if (!failedTurn?.query || agentLoading) return;
    submitAgentQuestion(failedTurn.query, { retryTurnId: failedTurn.id });
  };

  const handleAgentKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent?.isComposing) {
      e.preventDefault();
      if (!agentLoading && apiStatus !== 'offline' && agentQuery.trim()) {
        submitAgentQuestion(agentQuery);
      }
    }
  };

  const toggleMetadata = (index) => {
    setExpandedMeta((prev) => ({
      ...prev,
      [index]: !prev[index]
    }));
  };

  const handleCopyChunk = (index, rawText) => {
    const textToCopy =
      chunkViewMode === 'raw' ? rawText || '' : formatChunkPlainText(rawText);
    navigator.clipboard.writeText(textToCopy);
    setCopiedChunkIdx(index);
    setTimeout(() => {
      setCopiedChunkIdx((prev) => (prev === index ? null : prev));
    }, 1800);
  };

  const loadPrivacyInventory = async () => {
    setPrivacyLoading(true);
    setPrivacyError(null);
    try {
      const inv = await fetchUserDataInventory(authFetch);
      setPrivacyInventory(inv);
    } catch (err) {
      setPrivacyError(err.message || 'Could not load your personal data inventory.');
    } finally {
      setPrivacyLoading(false);
    }
  };

  const openPrivacyModal = () => {
    setPrivacyOpen(true);
    setPrivacyConfirmErase(false);
    setPrivacyError(null);
    loadPrivacyInventory();
  };

  const closePrivacyModal = () => {
    if (privacyErasing) return;
    setPrivacyOpen(false);
    setPrivacyConfirmErase(false);
    setPrivacyError(null);
  };

  const handleEraseMyData = async () => {
    if (privacyErasing) return;
    setPrivacyErasing(true);
    setPrivacyError(null);
    try {
      const result = await eraseAllUserData(authFetch);
      const uidToClear = currentUser?.uid;
      if (uidToClear && typeof window !== 'undefined') {
        window.sessionStorage.removeItem(getThreadStorageKey(uidToClear));
      }
      resetTenantState();
      if (uidToClear) {
        restoredUidRef.current = uidToClear;
      }
      setNotification({
        type: 'success',
        message:
          result?.message ||
          'All personal data, collections, archived PDFs, and conversation history have been permanently erased.',
      });
    } catch (err) {
      setPrivacyError(err.message || 'Failed to erase personal data. Please try again.');
    } finally {
      setPrivacyErasing(false);
    }
  };

  if (authInitializing) {
    return (
      <div className="auth-screen">
        <div className="glass-card" style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          <RefreshCw size={20} className="spinner" color="var(--primary)" />
          <span>Signing you in...</span>
        </div>
      </div>
    );
  }

  if (!currentUser) {
    return (
      <AuthGate
        onLocalDevLogin={(devUser) => {
          resetTenantState();
          setCurrentUser(devUser);
        }}
      />
    );
  }

  return (
    <div className="app-container">
      {/* Header */}
      <header className="app-header">
        <div className="brand">
          <div className="logo-icon">
            <Database size={28} />
          </div>
          <div>
            <h1 className="brand-title">Interactive RAG</h1>
            <p className="brand-subtitle">Upload PDFs, search chunks, and chat with your documents</p>
          </div>
        </div>

        <div className="header-actions">
          <div className="status-pill">
            <span className={`status-dot ${apiStatus === 'online' ? 'online' : 'offline'}`}></span>
            <span>{apiStatus === 'online' ? 'Connected' : 'Offline'}</span>
            <button onClick={fetchCollections} className="refresh-btn" title="Refresh connection">
              <RefreshCw size={14} className={apiStatus === 'checking' ? 'spinner' : ''} />
            </button>
          </div>

          <button
            type="button"
            className="privacy-header-btn"
            onClick={openPrivacyModal}
            title="Inspect or erase your stored personal data (GDPR Art. 15 & Art. 17)"
          >
            <Shield size={14} />
            <span>My Data &amp; Privacy</span>
          </button>

          <div className="user-pill">
            {currentUser.photoURL ? (
              <img
                src={currentUser.photoURL}
                alt={currentUser.displayName || currentUser.email || 'User'}
                className="user-avatar"
                referrerPolicy="no-referrer"
              />
            ) : (
              <User size={16} color="var(--primary)" />
            )}
            <span>{currentUser.email || currentUser.displayName || currentUser.uid}</span>
            <button type="button" onClick={handleSignOut} className="signout-btn" title="Sign out">
              <LogOut size={13} />
              <span>Sign Out</span>
            </button>
          </div>
        </div>
      </header>

      {/* Main Navigation Tabs */}
      <div className="nav-tabs">
        <button
          className={`nav-tab-btn ${activeTab === 'ingest' ? 'active' : ''}`}
          onClick={() => setActiveTab('ingest')}
        >
          <Upload size={18} />
          Upload Documents
        </button>
        <button
          className={`nav-tab-btn ${activeTab === 'query' ? 'active' : ''}`}
          onClick={() => setActiveTab('query')}
        >
          <Search size={18} />
          Search Chunks
        </button>
        <button
          className={`nav-tab-btn ${activeTab === 'agent' ? 'active' : ''}`}
          onClick={() => setActiveTab('agent')}
        >
          <Bot size={18} />
          AI Assistant
        </button>
        <button
          className={`nav-tab-btn ${activeTab === 'manage' ? 'active' : ''}`}
          onClick={() => setActiveTab('manage')}
        >
          <Database size={18} />
          My Collections
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
                Indexed {notification.details.chunks_added}{' '}
                {notification.details.chunks_added === 1 ? 'chunk' : 'chunks'} from{' '}
                {notification.details.processed_files?.length || 0} file(s)
                {notification.details.embedding_model
                  ? ` using ${notification.details.embedding_model}`
                  : ''}
                .
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
              Upload PDF Documents
            </h2>

            <form onSubmit={handleIngestSubmit}>
              {/* Step 1: Collection Selection */}
              <div className="form-group">
                <label className="form-label">Collection</label>
                
                <div className="tab-group">
                  <button
                    type="button"
                    className={`tab-btn ${collectionMode === 'existing' ? 'active' : ''}`}
                    onClick={() => {
                      setCollectionMode('existing');
                      const colEmb = getCollectionEmbeddingModel(selectedCollection);
                      if (colEmb) {
                        setIngestEmbeddingModel(colEmb);
                      }
                    }}
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
                        onChange={(e) => {
                          const nextCol = e.target.value;
                          setSelectedCollection(nextCol);
                          const colEmb = getCollectionEmbeddingModel(nextCol);
                          if (colEmb) {
                            setIngestEmbeddingModel(colEmb);
                          }
                        }}
                      >
                        {collections.map((col) => (
                          <option key={col} value={col}>
                            {col}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                        No collections yet. Select 'New Collection' to create one.
                      </p>
                    )}
                  </div>
                ) : (
                  <input
                    type="text"
                    className="form-input"
                    placeholder="e.g. annual-reports-2025, product-guides"
                    value={newCollectionName}
                    onChange={(e) => setNewCollectionName(e.target.value)}
                  />
                )}
              </div>

              {/* Embedding Model Selection */}
              <div className="form-group">
                <label className="form-label">Embedding Model</label>
                <select
                  className="form-select"
                  value={ingestEmbeddingModel}
                  onChange={(e) => setIngestEmbeddingModel(e.target.value)}
                  aria-label="Embedding model for document indexing"
                >
                  {availableEmbeddingModels.map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
                </select>
              </div>

              {/* Step 2: Document Source */}
              <div className="form-group">
                <label className="form-label">Files to Add</label>
                
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
                    Local File Paths
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
                      <p className="dropzone-text">Click or drag &amp; drop PDF files here</p>
                      <p className="dropzone-hint">Supports one or multiple PDF documents</p>
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
                      placeholder="Enter file path(s), one per line, e.g.:&#10;./data/annual_report_2024.pdf&#10;./data/annual_report_2025.pdf"
                      value={pdfPaths}
                      onChange={(e) => setPdfPaths(e.target.value)}
                    />
                    <p style={{ fontSize: '0.75rem', color: 'var(--text-dim)', marginTop: '4px' }}>
                      Enter local file paths available on the server, one per line.
                    </p>
                  </div>
                )}
              </div>

              {/* Step 3: Submit Button */}
              <button type="submit" className="btn-primary" disabled={loading || apiStatus === 'offline'}>
                {loading ? (
                  <>
                    <RefreshCw className="spinner" size={20} />
                    Processing &amp; Indexing PDFs...
                  </>
                ) : (
                  <>
                    <PlusCircle size={20} />
                    Add Documents to Collection
                  </>
                )}
              </button>
            </form>
          </div>
        )}

        {/* Search Chunks Tab Content */}
        {activeTab === 'query' && (
          <div className="glass-card">
            <h2 className="card-title">
              <Search size={22} color="var(--accent-cyan)" />
              Search Document Chunks
            </h2>

            <form onSubmit={handleQuerySubmit}>
              {/* Target Collection */}
              <div className="form-group">
                <label className="form-label">Collection</label>
                {collections.length > 0 ? (
                  <select
                    className="form-select"
                    value={queryCollection}
                    onChange={(e) => {
                      const nextCol = e.target.value;
                      setQueryCollection(nextCol);
                      const colEmb = getCollectionEmbeddingModel(nextCol);
                      if (colEmb) {
                        setQueryEmbeddingModel(colEmb);
                      }
                    }}
                  >
                    {collections.map((col) => (
                      <option key={col} value={col}>
                        {col}
                      </option>
                    ))}
                  </select>
                ) : (
                  <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                    No collections yet. Upload documents first to create one.
                  </p>
                )}
              </div>

              {/* Embedding Model Selection */}
              <div className="form-group">
                <label className="form-label">Embedding Model</label>
                <select
                  className="form-select"
                  value={queryEmbeddingModel}
                  onChange={(e) => setQueryEmbeddingModel(e.target.value)}
                  aria-label="Embedding model for similarity search"
                >
                  {availableEmbeddingModels.map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
                </select>
              </div>

              {/* Query Text */}
              <div className="form-group">
                <label className="form-label">What are you looking for?</label>
                <input
                  type="text"
                  className="form-input"
                  placeholder="e.g., key recommendations, budget overview, or project timeline"
                  value={queryText}
                  onChange={(e) => setQueryText(e.target.value)}
                />
              </div>

              {/* Number of Results selector */}
              <div className="form-group">
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
                  <label className="form-label" style={{ margin: 0 }}>Number of Results</label>
                  <span style={{ fontSize: '0.85rem', fontWeight: 700, color: 'var(--accent-cyan)' }}>
                    {topK} {Number(topK) === 1 ? 'chunk' : 'chunks'}
                  </span>
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
                    Searching Documents...
                  </>
                ) : (
                  <>
                    <Search size={20} />
                    Search Chunks
                  </>
                )}
              </button>
            </form>

            {/* Results Section */}
            {queryResults && (() => {
              const queryTerms = extractQueryTerms(submittedQuery);
              return (
                <div className="results-section">
                  <div className="results-toolbar">
                    <div className="results-toolbar-left">
                      <h3 className="results-heading">
                        Matching Chunks
                        <span className="results-count-pill">{queryResults.length}</span>
                      </h3>
                      <span className="results-collection-label">
                        in <strong>{queryCollection}</strong>
                      </span>
                    </div>

                    {queryResults.length > 0 && (
                      <div className="results-toolbar-controls">
                        {queryTerms.length > 0 && (
                          <button
                            type="button"
                            className={`chunk-control-pill ${highlightMatches ? 'active' : ''}`}
                            onClick={() => setHighlightMatches((prev) => !prev)}
                            title="Highlight matching search words"
                          >
                            <Highlighter size={13} />
                            <span>Highlights</span>
                          </button>
                        )}

                        <div className="chunk-view-segmented" role="group" aria-label="Chunk layout">
                          <button
                            type="button"
                            className={`chunk-view-btn ${chunkViewMode === 'reflowed' ? 'active' : ''}`}
                            onClick={() => setChunkViewMode('reflowed')}
                            title="Easy-to-read paragraphs"
                          >
                            <AlignLeft size={13} />
                            <span>Formatted</span>
                          </button>
                          <button
                            type="button"
                            className={`chunk-view-btn ${chunkViewMode === 'raw' ? 'active' : ''}`}
                            onClick={() => setChunkViewMode('raw')}
                            title="Original text layout from the PDF"
                          >
                            <FileText size={13} />
                            <span>Original</span>
                          </button>
                        </div>
                      </div>
                    )}
                  </div>

                  {queryResults.length === 0 ? (
                    <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', margin: '1.5rem 0' }}>
                      No matching chunks found. Try phrasing your search differently.
                    </p>
                  ) : (
                    <div className="results-container">
                      {queryResults.map((res, idx) => {
                        const rawContent = res.page_content || '';
                        const wordCount = rawContent.trim()
                          ? rawContent.trim().split(/\s+/).length
                          : 0;
                        const sourceName = res.metadata?.source
                          ? res.metadata.source.split(/[\\/]/).pop()
                          : null;
                        const pageDisplay =
                          res.metadata?.page_label !== undefined
                            ? res.metadata.page_label
                            : res.metadata?.page !== undefined
                            ? Number(res.metadata.page) + 1
                            : null;
                        const isCopied = copiedChunkIdx === idx;

                        return (
                          <div key={idx} className="result-card">
                            <div className="result-header">
                              <div className="result-header-left">
                                <span className="result-rank-badge">
                                  Chunk #{idx + 1}
                                </span>
                                <span className="result-stats-pill">
                                  {wordCount} words
                                </span>
                              </div>

                              <div className="result-header-right">
                                {sourceName && (
                                  <span className="result-source-tag" title={res.metadata.source}>
                                    <FileText size={13} className="result-source-icon" />
                                    <span className="result-source-name">{sourceName}</span>
                                    {pageDisplay !== null && (
                                      <span className="result-page-badge">p. {pageDisplay}</span>
                                    )}
                                  </span>
                                )}

                                <button
                                  type="button"
                                  className={`chunk-copy-btn ${isCopied ? 'copied' : ''}`}
                                  onClick={() => handleCopyChunk(idx, rawContent)}
                                  title={
                                    isCopied
                                      ? 'Copied!'
                                      : `Copy ${chunkViewMode === 'raw' ? 'original' : 'formatted'} chunk`
                                  }
                                >
                                  {isCopied ? <Check size={13} /> : <Copy size={13} />}
                                  <span>{isCopied ? 'Copied' : 'Copy'}</span>
                                </button>
                              </div>
                            </div>

                            <FormattedChunkView
                              rawText={rawContent}
                              queryTerms={queryTerms}
                              viewMode={chunkViewMode}
                              highlightEnabled={highlightMatches}
                            />

                            {res.metadata && (
                              <div className="result-footer">
                                <button
                                  type="button"
                                  className="result-metadata-toggle"
                                  onClick={() => toggleMetadata(idx)}
                                >
                                  {expandedMeta[idx] ? (
                                    <>
                                      <ChevronUp size={14} /> Hide File Details
                                    </>
                                  ) : (
                                    <>
                                      <ChevronDown size={14} /> File Details
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
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })()}
          </div>
        )}

        {/* AI Assistant Tab Content */}
        {activeTab === 'agent' &&
          (() => {
            const effectiveCollection =
              activeThread?.collection || agentCollection || selectedCollection || '';
            const activeTurns = activeThread?.turns || [];
            const showThreadStrip =
              agentThreads.length > 1 || activeTurns.length > 0;
            const distinctThreadCollections = new Set(
              agentThreads.map((t) => t.collection).filter(Boolean)
            );

            return (
              <div className="glass-card agent-workspace-card">
                {/* Workspace Header */}
                <div className="agent-workspace-header">
                  <div className="agent-workspace-title-group">
                    <h2 className="card-title" style={{ margin: 0 }}>
                      <Bot size={22} color="var(--primary)" />
                      <span>AI Assistant</span>
                    </h2>
                  </div>

                  <div className="agent-workspace-header-actions">
                    <div
                      className="agent-main-view-toggle"
                      role="tablist"
                      aria-label="Assistant view"
                    >
                      <button
                        type="button"
                        role="tab"
                        aria-selected={agentViewMode === 'chat'}
                        className={`agent-main-view-btn ${
                          agentViewMode === 'chat' ? 'active' : ''
                        }`}
                        onClick={() => setAgentViewMode('chat')}
                      >
                        <MessageSquare size={14} />
                        <span>Chat</span>
                      </button>
                      <button
                        type="button"
                        role="tab"
                        aria-selected={agentViewMode === 'steps'}
                        className={`agent-main-view-btn ${
                          agentViewMode === 'steps' ? 'active' : ''
                        }`}
                        onClick={() => setAgentViewMode('steps')}
                        title="See how the assistant searched your documents for each question"
                      >
                        <Search size={14} />
                        <span>Search Steps</span>
                        {activeTurns.length > 0 && (
                          <span className="agent-view-toggle-count">
                            {activeTurns.length}
                          </span>
                        )}
                      </button>
                    </div>

                    <button
                      type="button"
                      className="btn-new-thread"
                      onClick={handleNewThread}
                      disabled={agentLoading || (activeTurns.length === 0 && agentThreads.length <= 1)}
                      title="Start a new chat"
                    >
                      <MessageSquarePlus size={15} />
                      <span>New Chat</span>
                    </button>
                  </div>
                </div>

                <p className="agent-workspace-subtitle">
                  Ask questions about your documents and follow up naturally — the assistant
                  remembers your conversation and cites the exact pages it used.
                </p>

                {/* Session Config Bar: Collection + Models + Response Mode */}
                <div className="agent-session-bar">
                  <div className="agent-collection-picker">
                    <span className="agent-picker-label">
                      <Database size={14} color="var(--accent-cyan)" />
                      <span>Collection</span>
                    </span>
                    {collections.length > 0 ? (
                      <select
                        className="form-select agent-compact-select"
                        value={effectiveCollection}
                        onChange={(e) => selectAgentCollection(e.target.value)}
                        disabled={agentLoading}
                        aria-label="Choose collection"
                      >
                        {collections.map((col) => (
                          <option key={col} value={col}>
                            {col}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <span className="agent-no-collection-hint">
                        No collections yet — upload PDFs first
                      </span>
                    )}
                  </div>

                  <div className="agent-session-bar-right">
                    <div className="agent-model-picker">
                      <span className="agent-picker-label">
                        <Sparkles size={13} color="var(--primary)" />
                        <span>LLM</span>
                      </span>
                      <select
                        className="form-select agent-compact-select"
                        value={agentLlmModel}
                        onChange={(e) => setAgentLlmModel(e.target.value)}
                        disabled={agentLoading}
                        aria-label="Choose LLM model"
                      >
                        {availableLlmModels.map((model) => (
                          <option key={model} value={model}>
                            {model}
                          </option>
                        ))}
                      </select>
                    </div>

                    <div className="agent-model-picker">
                      <span className="agent-picker-label">
                        <Layers size={13} color="var(--accent-cyan)" />
                        <span>Embedding</span>
                      </span>
                      <select
                        className="form-select agent-compact-select"
                        value={agentEmbeddingModel}
                        onChange={(e) => setAgentEmbeddingModel(e.target.value)}
                        disabled={agentLoading}
                        aria-label="Choose embedding model"
                      >
                        {availableEmbeddingModels.map((model) => (
                          <option key={model} value={model}>
                            {model}
                          </option>
                        ))}
                      </select>
                    </div>

                    <div
                      className="chunk-view-segmented"
                      role="group"
                      aria-label="Response delivery mode"
                    >
                      <button
                        type="button"
                        className={`chunk-view-btn ${
                          agentEndpointMode === 'stream' ? 'active' : ''
                        }`}
                        onClick={() => setAgentEndpointMode('stream')}
                        title="Show progress and answers live as they are generated"
                      >
                        <Zap size={12} />
                        <span>Live</span>
                      </button>
                      <button
                        type="button"
                        className={`chunk-view-btn ${
                          agentEndpointMode === 'standard' ? 'active' : ''
                        }`}
                        onClick={() => setAgentEndpointMode('standard')}
                        title="Show the complete answer once ready"
                      >
                        <FileCheck size={12} />
                        <span>Standard</span>
                      </button>
                    </div>
                  </div>
                </div>

                {/* Recent Chats Switcher Strip */}
                {showThreadStrip && (
                  <div className="agent-threads-strip">
                    <div className="agent-threads-strip-label">
                      <History size={13} />
                      <span>Chats</span>
                    </div>
                    <div className="agent-threads-list" role="tablist" aria-label="Recent chats">
                      {agentThreads.map((thread) => {
                        const isActive = thread.id === activeThread?.id;
                        const turnCount = thread.turns.length;
                        const canDelete = agentThreads.length > 1 || turnCount > 0;
                        return (
                          <div
                            key={thread.id}
                            role="tab"
                            aria-selected={isActive}
                            tabIndex={0}
                            className={`agent-thread-chip ${isActive ? 'active' : ''} ${
                              agentLoading && !isActive ? 'disabled' : ''
                            }`}
                            onClick={() => handleSwitchThread(thread)}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter' || e.key === ' ') {
                                e.preventDefault();
                                handleSwitchThread(thread);
                              }
                            }}
                            title={`${thread.title}${
                              thread.collection ? ` (${thread.collection})` : ''
                            }`}
                          >
                            <MessageSquare size={12} className="agent-thread-chip-icon" />
                            <span className="agent-thread-chip-title">
                              {thread.title}
                            </span>
                            {(distinctThreadCollections.size > 1 ||
                              (thread.collection &&
                                thread.collection !== effectiveCollection)) &&
                              thread.collection && (
                                <span className="agent-thread-chip-col">
                                  {thread.collection}
                                </span>
                              )}
                            {turnCount > 0 && (
                              <span className="agent-thread-chip-count">{turnCount}</span>
                            )}
                            {canDelete && (
                              <button
                                type="button"
                                className="agent-thread-chip-close"
                                onClick={(e) => handleDeleteThread(e, thread.id)}
                                disabled={agentLoading}
                                title={
                                  agentThreads.length === 1
                                    ? 'Clear chat'
                                    : 'Remove chat'
                                }
                                aria-label="Remove chat"
                              >
                                <X size={11} />
                              </button>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}

                {agentViewMode === 'steps' ? (
                  <AgentSearchStepsView
                    turns={activeTurns}
                    focusedTurnId={focusedStepTurnId}
                    onSelectTurnFilter={setFocusedStepTurnId}
                    onBackToChat={() => setAgentViewMode('chat')}
                  />
                ) : (
                  <>
                    {/* Conversation Timeline */}
                    <div className="agent-chat-feed">
                      {activeTurns.length === 0 ? (
                        <div className="agent-empty-thread">
                          <div className="agent-empty-icon-wrap">
                            <MessageSquare size={22} />
                          </div>
                          <h3 className="agent-empty-title">
                            Ask Anything About Your Documents
                          </h3>
                          <p className="agent-empty-desc">
                            Ask a question
                            {effectiveCollection ? (
                              <>
                                {' '}
                                about <strong>{effectiveCollection}</strong>
                              </>
                            ) : null}{' '}
                            below, or choose an example to get started. Follow-up questions
                            automatically keep context from earlier answers.
                          </p>
                          {effectiveCollection && (
                            <div className="agent-starter-chips">
                              {STARTER_PROMPTS.map((promptText, pIdx) => (
                                <button
                                  key={pIdx}
                                  type="button"
                                  className="agent-starter-chip"
                                  onClick={() => {
                                    setAgentQuery(promptText);
                                    agentInputRef.current?.focus();
                                  }}
                                >
                                  <Sparkles size={12} className="agent-starter-chip-icon" />
                                  <span>{promptText}</span>
                                </button>
                              ))}
                            </div>
                          )}
                        </div>
                      ) : (
                        <div className="agent-turns-timeline">
                          {activeTurns.map((turn, idx) => (
                            <ConversationTurnCard
                              key={turn.id}
                              turn={turn}
                              turnIndex={idx}
                              priorTurns={activeTurns.slice(0, idx)}
                              onRetryTurn={handleRetryTurn}
                              onStopTurn={
                                turn.status === 'streaming' ? handleStopAgent : null
                              }
                              onOpenSteps={handleOpenTurnSteps}
                              agentLoading={agentLoading}
                            />
                          ))}
                          <div ref={conversationEndRef} />
                        </div>
                      )}
                    </div>

                    {/* Question Composer */}
                    <form className="agent-composer-form" onSubmit={handleAgentSubmit}>
                      <div className="agent-composer-header">
                        {activeTurns.length > 0 ? (
                          <span className="composer-memory-cue follow-up">
                            <CornerDownLeft size={13} />
                            <span>
                              Follow-up question
                              {effectiveCollection ? (
                                <>
                                  {' '}
                                  in <code>{effectiveCollection}</code>
                                </>
                              ) : null}
                            </span>
                          </span>
                        ) : (
                          <span className="composer-memory-cue">
                            <span>Your question</span>
                          </span>
                        )}

                        {activeTurns.length > 0 && !agentLoading && (
                          <button
                            type="button"
                            className="composer-reset-link"
                            onClick={handleNewThread}
                          >
                            <PlusCircle size={12} />
                            <span>New chat</span>
                          </button>
                        )}
                      </div>

                      <textarea
                        ref={agentInputRef}
                        rows={activeTurns.length > 0 ? 2 : 3}
                        className="form-textarea agent-composer-textarea"
                        placeholder={
                          activeTurns.length > 0
                            ? 'Ask a follow-up question (e.g., Explain the second point in more detail, or compare those numbers in a table)…'
                            : 'Ask a question about your documents (e.g., Summarize the main takeaways and cite the pages)…'
                        }
                        value={agentQuery}
                        onChange={(e) => setAgentQuery(e.target.value)}
                        onKeyDown={handleAgentKeyDown}
                        disabled={apiStatus === 'offline'}
                      />

                      <div className="agent-composer-footer">
                        <span className="composer-shortcut-hint">
                          Press <kbd>Enter</kbd> to send · <kbd>Shift</kbd> +{' '}
                          <kbd>Enter</kbd> for new line
                        </span>

                        {agentLoading ? (
                          <button
                            type="button"
                            className="btn-stop-agent"
                            onClick={handleStopAgent}
                          >
                            <Square size={14} />
                            <span>Stop</span>
                          </button>
                        ) : (
                          <button
                            type="submit"
                            className="btn-send-agent"
                            disabled={
                              apiStatus === 'offline' ||
                              !effectiveCollection ||
                              !agentQuery.trim()
                            }
                          >
                            <Send size={15} />
                            <span>
                              {activeTurns.length > 0 ? 'Send' : 'Ask'}
                            </span>
                          </button>
                        )}
                      </div>
                    </form>
                  </>
                )}
              </div>
            );
          })()}

        {/* My Collections Tab View */}
        {activeTab === 'manage' && (
          <div className="glass-card">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
              <h2 className="card-title" style={{ margin: 0 }}>
                <Database size={22} color="var(--primary)" />
                My Collections
              </h2>
              <button onClick={fetchCollections} className="refresh-btn" title="Refresh collections">
                <RefreshCw size={16} className={apiStatus === 'checking' ? 'spinner' : ''} />
              </button>
            </div>
            <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginBottom: '1.5rem' }}>
              Browse your document collections, search chunks, start a chat, or remove collections you no longer need.
            </p>

            {collectionDetails.length === 0 ? (
              <div style={{ padding: '3rem 1.5rem', textAlign: 'center', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-md)', border: '1px dashed var(--border-color)' }}>
                <Database size={40} color="var(--text-muted)" style={{ marginBottom: '1rem', opacity: 0.5 }} />
                <p style={{ color: 'var(--text-muted)', fontSize: '0.95rem' }}>You do not have any document collections yet.</p>
                <button 
                  className="btn-primary" 
                  style={{ marginTop: '1rem', display: 'inline-flex', width: 'auto', padding: '0.6rem 1.2rem' }}
                  onClick={() => setActiveTab('ingest')}
                >
                  <Upload size={16} />
                  Upload Documents
                </button>
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
                {collectionDetails.map((col) => {
                  const isConfirming = deletingCol === col.name;
                  return (
                    <div 
                      key={col.name}
                      style={{
                        padding: '1.25rem',
                        borderRadius: 'var(--radius-md)',
                        background: isConfirming ? 'rgba(239, 68, 68, 0.08)' : 'rgba(255, 255, 255, 0.03)',
                        border: `1px solid ${isConfirming ? 'rgba(239, 68, 68, 0.4)' : 'var(--border-color)'}`,
                        transition: 'all 0.2s ease',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: '1rem'
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                          <div style={{ padding: '0.6rem', borderRadius: '8px', background: 'rgba(99, 102, 241, 0.15)', color: 'var(--primary)' }}>
                            <Layers size={20} />
                          </div>
                          <div>
                            <h3 style={{ fontSize: '1rem', fontWeight: 600, color: '#fff', margin: 0 }}>
                              {col.name}
                            </h3>
                            <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', margin: '2px 0 0 0' }}>
                              {col.count !== undefined
                                ? `${col.count} ${col.count === 1 ? 'chunk' : 'chunks'} indexed`
                                : 'Document collection'}
                              {col.embedding_model ? ` · ${col.embedding_model}` : ''}
                            </p>
                          </div>
                        </div>

                        {!isConfirming && (
                          <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                            <button
                              type="button"
                              className="tab-btn"
                              style={{ padding: '0.4rem 0.8rem', fontSize: '0.8rem' }}
                              onClick={() => {
                                setQueryCollection(col.name);
                                const colEmb = getCollectionEmbeddingModel(col.name);
                                if (colEmb) {
                                  setQueryEmbeddingModel(colEmb);
                                }
                                setActiveTab('query');
                              }}
                            >
                              <Search size={14} />
                              Search
                            </button>
                            <button
                              type="button"
                              className="tab-btn"
                              style={{ padding: '0.4rem 0.8rem', fontSize: '0.8rem' }}
                              onClick={() => {
                                selectAgentCollection(col.name);
                                setActiveTab('agent');
                              }}
                            >
                              <Bot size={14} />
                              Ask AI
                            </button>
                            <button
                              type="button"
                              className="tab-btn"
                              style={{ 
                                padding: '0.4rem 0.8rem', 
                                fontSize: '0.8rem',
                                color: '#f87171',
                                borderColor: 'rgba(248, 113, 113, 0.3)',
                                background: 'rgba(248, 113, 113, 0.1)'
                              }}
                              onClick={() => setDeletingCol(col.name)}
                            >
                              <Trash2 size={14} />
                              Delete
                            </button>
                          </div>
                        )}
                      </div>

                      {/* Confirmation Banner if deleting */}
                      {isConfirming && (
                        <div style={{ 
                          padding: '0.85rem 1rem', 
                          borderRadius: '8px', 
                          background: 'rgba(239, 68, 68, 0.15)', 
                          border: '1px solid rgba(239, 68, 68, 0.3)',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          gap: '1rem'
                        }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: '#fca5a5', fontSize: '0.85rem' }}>
                            <AlertTriangle size={18} style={{ flexShrink: 0 }} />
                            <span>Are you sure you want to delete <strong>"{col.name}"</strong>?</span>
                          </div>
                          <div style={{ display: 'flex', gap: '0.5rem', flexShrink: 0 }}>
                            <button
                              type="button"
                              className="tab-btn"
                              style={{ padding: '0.35rem 0.75rem', fontSize: '0.8rem' }}
                              onClick={() => setDeletingCol(null)}
                              disabled={loading}
                            >
                              Cancel
                            </button>
                            <button
                              type="button"
                              style={{
                                padding: '0.35rem 0.75rem',
                                fontSize: '0.8rem',
                                background: '#ef4444',
                                color: '#fff',
                                border: 'none',
                                borderRadius: '6px',
                                fontWeight: 600,
                                cursor: 'pointer',
                                display: 'flex',
                                alignItems: 'center',
                                gap: '0.35rem'
                              }}
                              onClick={() => handleDeleteCollection(col.name)}
                              disabled={loading}
                            >
                              {loading ? (
                                <RefreshCw className="spinner" size={14} />
                              ) : (
                                <Trash2 size={14} />
                              )}
                              Delete Collection
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {/* Right Section - Collections Sidebar */}
        <div className="glass-card">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem' }}>
            <h3 style={{ fontSize: '1.1rem', fontWeight: 600, color: '#fff', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <Layers size={18} color="var(--accent-cyan)" />
              Collections
            </h3>
            <button onClick={fetchCollections} className="refresh-btn" title="Refresh collections">
              <RefreshCw size={14} className={apiStatus === 'checking' ? 'spinner' : ''} />
            </button>
          </div>

          {collections.length === 0 ? (
            <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', textAlign: 'center', padding: '2rem 0' }}>
              No collections yet. Upload your first PDF document to get started.
            </p>
          ) : (
            <div>
              {collections.map((col) => {
                const detail = collectionDetails.find((d) => d.name === col);
                const isSelected =
                  (activeTab === 'ingest' && selectedCollection === col) ||
                  (activeTab === 'query' && queryCollection === col) ||
                  (activeTab === 'agent' && (activeThread?.collection || agentCollection) === col);

                return (
                  <div 
                    key={col} 
                    className="collection-card-item"
                    style={{
                      borderColor: isSelected ? 'var(--primary)' : 'var(--border-color)',
                      cursor: 'pointer'
                    }}
                    onClick={() => {
                      setSelectedCollection(col);
                      setQueryCollection(col);
                      const colEmb = getCollectionEmbeddingModel(col);
                      if (colEmb) {
                        if (collectionMode === 'existing') {
                          setIngestEmbeddingModel(colEmb);
                        }
                        setQueryEmbeddingModel(colEmb);
                      }
                      if (!agentLoading) {
                        selectAgentCollection(col);
                      }
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
                      <FileText size={16} color={isSelected ? 'var(--primary)' : 'var(--text-muted)'} />
                      <span style={{ fontWeight: 500, fontSize: '0.9rem' }}>{col}</span>
                    </div>
                    <span className="collection-badge">
                      {detail?.count !== undefined
                        ? `${detail.count} ${detail.count === 1 ? 'chunk' : 'chunks'}`
                        : isSelected
                        ? 'Selected'
                        : 'Ready'}
                    </span>
                  </div>
                );
              })}
            </div>
          )}

          <div style={{ marginTop: '2rem', padding: '1rem', background: 'rgba(255,255,255,0.03)', borderRadius: 'var(--radius-md)', border: '1px solid var(--border-color)' }}>
            <h4 style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-muted)', marginBottom: '0.5rem' }}>
              💡 Tip
            </h4>
            <p style={{ fontSize: '0.8rem', color: 'var(--text-dim)', lineHeight: 1.4 }}>
              Select any collection to search specific chunks or open <strong>AI Assistant</strong> to ask questions, follow up naturally, and click any page reference to preview the source text.
            </p>
          </div>
        </div>
      </div>

      <PrivacyDataModal
        isOpen={privacyOpen}
        onClose={closePrivacyModal}
        inventory={privacyInventory}
        loading={privacyLoading}
        erasing={privacyErasing}
        error={privacyError}
        confirmErase={privacyConfirmErase}
        setConfirmErase={setPrivacyConfirmErase}
        onRefresh={loadPrivacyInventory}
        onConfirmErase={handleEraseMyData}
      />
    </div>
  );
}
