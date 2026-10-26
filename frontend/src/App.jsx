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
  ChevronDown,
  ChevronUp,
  Bot,
  Terminal,
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
  Zap
} from 'lucide-react';
import {
  API_BASE,
  AGENT_API_BASE,
  subscribeToAuthChanges,
  logoutUser,
} from './firebase';
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
                    title={`Click to ${isActive ? 'hide' : 'preview'} retrieved passage from ${cit.source}${cit.pageLabel ? ` (${cit.pageLabel})` : ''}`}
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
        <span style={{ color: 'var(--text-muted)' }}>Processing query response...</span>
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
              Sources Cited ({citationsSummary.length})
            </span>
            {retrievedChunks.length > 0 && (
              <span className="agent-sources-hint">
                Click a citation or page to inspect retrieved passage
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
                            title={`Preview retrieved passage for ${srcItem.source} (p. ${p})`}
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
                        View passage
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
                  <span>Grounded Evidence — {selectedCitation.citation.source}</span>
                  {selectedCitation.citation.pageLabel && (
                    <span className="result-page-badge">
                      {selectedCitation.citation.pageLabel}
                    </span>
                  )}
                  {!previewMatch.exact && (
                    <span className="agent-citation-fallback-note">
                      (showing retrieved chunk(s) from this document)
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
                        <span>Retrieved Chunk #{cIdx + 1}</span>
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
                      <div className="pretty-msg-box pretty-ai-markdown">
                        <AgentMarkdownView
                          markdown={msg.content}
                          events={events}
                          compact
                        />
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
                const xmlChunks = parseRetrievedChunksXml(msg.content);
                return (
                  <div key={index}>
                    <div className="pretty-header tool">
                      Tool Message
                    </div>
                    <div style={{ fontSize: '0.82rem', marginBottom: '6px', fontWeight: 600, color: '#f87171' }}>
                      Tool Name: {msg.name || 'Retriever'}
                    </div>
                    <div style={{ fontSize: '0.8rem', fontWeight: 600, color: '#e2e8f0', marginBottom: '6px' }}>
                      Tool Result {xmlChunks.length > 0 ? `(${xmlChunks.length} retrieved chunks)` : ''}:
                    </div>
                    {xmlChunks.length > 0 ? (
                      <div className="pretty-tool-chunks-list">
                        {xmlChunks.map((chunk, cIdx) => (
                          <div key={cIdx} className="pretty-tool-chunk-card">
                            <div className="pretty-tool-chunk-header">
                              <span className="pretty-tool-chunk-index">Chunk #{cIdx + 1}</span>
                              <span className="result-source-tag">
                                <FileText size={12} className="result-source-icon" />
                                <span className="result-source-name">{chunk.source}</span>
                                <span className="result-page-badge">p. {chunk.page}</span>
                              </span>
                            </div>
                            <FormattedChunkView
                              rawText={chunk.content}
                              queryTerms={[]}
                              viewMode="reflowed"
                              highlightEnabled={false}
                            />
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="pretty-tool-result-box">
                        {msg.content}
                      </div>
                    )}
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
  const [currentUser, setCurrentUser] = useState(null);
  const [authInitializing, setAuthInitializing] = useState(true);

  const [activeTab, setActiveTab] = useState('ingest'); // 'ingest' | 'query' | 'agent' | 'manage'

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

  // Agent tab state
  const [agentCollection, setAgentCollection] = useState('');
  const [agentQuery, setAgentQuery] = useState('');
  const [submittedAgentQuery, setSubmittedAgentQuery] = useState('');
  const [agentEndpointMode, setAgentEndpointMode] = useState('stream'); // 'stream' | 'standard'
  const [agentViewMode, setAgentViewMode] = useState('rendered'); // 'rendered' | 'raw'
  const [agentCopied, setAgentCopied] = useState(false);
  const [agentLoading, setAgentLoading] = useState(false);
  const [agentAnswer, setAgentAnswer] = useState(null);
  const [agentEvents, setAgentEvents] = useState([]);
  const [showStreamEvents, setShowStreamEvents] = useState(false);

  const fileInputRef = useRef(null);
  const threadIdRef = useRef(`session-${Date.now()}`);

  const resetTenantState = () => {
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
    setAgentCollection('');
    setAgentQuery('');
    setSubmittedAgentQuery('');
    setAgentAnswer(null);
    setAgentEvents([]);
    setAgentCopied(false);
    setShowStreamEvents(false);
    setNotification(null);
    threadIdRef.current = `session-${Date.now()}`;
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
    try {
      if (!currentUser?.isLocalDev) {
        await logoutUser();
      }
    } finally {
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

  // Fetch collections from backend
  const fetchCollections = async () => {
    if (!currentUser) return;
    try {
      setApiStatus('checking');
      let res;
      try {
        res = await authFetch(`${API_BASE}/collections`);
      } catch (e) {
        res = await authFetch(`${AGENT_API_BASE}/collections`);
      }
      if (!res.ok) throw new Error('Failed to fetch collections');
      const data = await res.json();
      const colList = data.collections || [];
      const details = data.details || colList.map(c => ({ name: c, count: 0 }));
      setCollections(colList);
      setCollectionDetails(details);
      setApiStatus('online');
      
      if (colList.length > 0) {
        if (!selectedCollection || !colList.includes(selectedCollection)) setSelectedCollection(colList[0]);
        if (!queryCollection || !colList.includes(queryCollection)) setQueryCollection(colList[0]);
        if (!agentCollection || !colList.includes(agentCollection)) setAgentCollection(colList[0]);
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
        message: `Could not connect to backend API (${API_BASE}). Please ensure backend is running and authenticated.`
      });
    }
  };

  const handleDeleteCollection = async (colName) => {
    setNotification(null);
    setLoading(true);
    try {
      let res;
      try {
        res = await authFetch(`${API_BASE}/collections/${encodeURIComponent(colName)}`, {
          method: 'DELETE',
        });
      } catch (e) {
        res = await authFetch(`${AGENT_API_BASE}/collections/${encodeURIComponent(colName)}`, {
          method: 'DELETE',
        });
      }
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to delete collection');
      }
      setNotification({
        type: 'success',
        message: `Successfully deleted collection '${colName}'.`
      });
      setDeletingCol(null);
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
    if (currentUser?.uid) {
      fetchCollections();
    }
  }, [currentUser?.uid]);

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

      const response = await authFetch(`${API_BASE}/ingest`, {
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
    setExpandedMeta({});
    setCopiedChunkIdx(null);

    if (!queryCollection) {
      setNotification({
        type: 'error',
        message: 'Please select a collection to query.'
      });
      return;
    }

    const trimmedQuery = queryText.trim();
    if (!trimmedQuery) {
      setNotification({
        type: 'error',
        message: 'Please enter a search query.'
      });
      return;
    }

    setQueryLoading(true);

    try {
      const response = await authFetch(`${API_BASE}/query`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          query: trimmedQuery,
          collection_name: queryCollection,
          k: Number(topK)
        }),
      });

      const result = await response.json();

      if (!response.ok) {
        throw new Error(result.detail || 'Failed to query collection');
      }

      setSubmittedQuery(trimmedQuery);
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
    setAgentCopied(false);
    setShowStreamEvents(false);

    const targetCollection = agentCollection || selectedCollection;
    if (!targetCollection) {
      setNotification({
        type: 'error',
        message: 'Please select a collection for the Agentic RAG assistant.'
      });
      return;
    }

    const trimmedQuestion = agentQuery.trim();
    if (!trimmedQuestion) {
      setNotification({
        type: 'error',
        message: 'Please enter a question for the agent.'
      });
      return;
    }

    setSubmittedAgentQuery(trimmedQuestion);
    setAgentLoading(true);

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
              thread_id: threadIdRef.current,
              stream: false,
            }
          : {
              query: trimmedQuestion,
              collection_name: targetCollection,
              thread_id: threadIdRef.current,
            };

      const response = await authFetch(endpointUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
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
        const accumulatedEvents = [];

        const processLine = (rawLine) => {
          const trimmed = rawLine.trim();
          if (!trimmed.startsWith('data: ')) return false;

          const dataStr = trimmed.slice(6).trim();
          if (dataStr === '[DONE]') return true;

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

  const handleCopyChunk = (index, rawText) => {
    const textToCopy =
      chunkViewMode === 'raw' ? rawText || '' : formatChunkPlainText(rawText);
    navigator.clipboard.writeText(textToCopy);
    setCopiedChunkIdx(index);
    setTimeout(() => {
      setCopiedChunkIdx((prev) => (prev === index ? null : prev));
    }, 1800);
  };

  const handleCopyAgentAnswer = () => {
    if (!agentAnswer) return;
    navigator.clipboard.writeText(agentAnswer);
    setAgentCopied(true);
    setTimeout(() => setAgentCopied(false), 1800);
  };

  if (authInitializing) {
    return (
      <div className="auth-screen">
        <div className="glass-card" style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          <RefreshCw size={20} className="spinner" color="var(--primary)" />
          <span>Checking authentication session...</span>
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
            <h1 className="brand-title">Interactive RAG Portal</h1>
            <p className="brand-subtitle">Vector Store Ingestion & Semantic Retrieval Engine</p>
          </div>
        </div>

        <div className="header-actions">
          <div className="status-pill">
            <span className={`status-dot ${apiStatus === 'online' ? 'online' : 'offline'}`}></span>
            <span>{apiStatus === 'online' ? 'Backend Connected' : 'Backend Offline'}</span>
            <button onClick={fetchCollections} className="refresh-btn" title="Check connection">
              <RefreshCw size={14} className={apiStatus === 'checking' ? 'spinner' : ''} />
            </button>
          </div>

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
        <button
          className={`nav-tab-btn ${activeTab === 'manage' ? 'active' : ''}`}
          onClick={() => setActiveTab('manage')}
        >
          <Database size={18} />
          Manage Collections
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
            {queryResults && (() => {
              const queryTerms = extractQueryTerms(submittedQuery);
              return (
                <div className="results-section">
                  <div className="results-toolbar">
                    <div className="results-toolbar-left">
                      <h3 className="results-heading">
                        Search Results
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
                            title="Toggle query keyword highlighting"
                          >
                            <Highlighter size={13} />
                            <span>Highlights</span>
                          </button>
                        )}

                        <div className="chunk-view-segmented" role="group" aria-label="Chunk display mode">
                          <button
                            type="button"
                            className={`chunk-view-btn ${chunkViewMode === 'reflowed' ? 'active' : ''}`}
                            onClick={() => setChunkViewMode('reflowed')}
                            title="Reflow PDF line breaks into full-width paragraphs"
                          >
                            <AlignLeft size={13} />
                            <span>Reflowed</span>
                          </button>
                          <button
                            type="button"
                            className={`chunk-view-btn ${chunkViewMode === 'raw' ? 'active' : ''}`}
                            onClick={() => setChunkViewMode('raw')}
                            title="Show exact raw chunk text with original newlines"
                          >
                            <Code2 size={13} />
                            <span>Raw</span>
                          </button>
                        </div>
                      </div>
                    )}
                  </div>

                  {queryResults.length === 0 ? (
                    <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', margin: '1.5rem 0' }}>
                      No matching documents found for your query.
                    </p>
                  ) : (
                    <div className="results-container">
                      {queryResults.map((res, idx) => {
                        const rawContent = res.page_content || '';
                        const wordCount = rawContent.trim()
                          ? rawContent.trim().split(/\s+/).length
                          : 0;
                        const charCount = rawContent.length;
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
                                  Result #{idx + 1}
                                </span>
                                <span className="result-stats-pill">
                                  {wordCount} words · {charCount} chars
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
                                      : `Copy ${chunkViewMode === 'raw' ? 'raw' : 'formatted'} chunk`
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
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })()}
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

              {/* Natural Language Question & Endpoint Mode */}
              <div className="form-group">
                <div className="agent-question-label-row">
                  <label className="form-label" style={{ margin: 0 }}>
                    Question for AI Agent
                  </label>
                  <div className="chunk-view-segmented" role="group" aria-label="Agent endpoint mode">
                    <button
                      type="button"
                      className={`chunk-view-btn ${agentEndpointMode === 'stream' ? 'active' : ''}`}
                      onClick={() => setAgentEndpointMode('stream')}
                      title="Stream real-time SSE events via POST /agent/stream"
                    >
                      <Zap size={12} />
                      <span>/agent/stream</span>
                    </button>
                    <button
                      type="button"
                      className={`chunk-view-btn ${agentEndpointMode === 'standard' ? 'active' : ''}`}
                      onClick={() => setAgentEndpointMode('standard')}
                      title="Fetch complete JSON response via POST /agent"
                    >
                      <Server size={12} />
                      <span>/agent</span>
                    </button>
                  </div>
                </div>
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
                  <div className="agent-response-title-group">
                    <div className="agent-response-title">
                      <Sparkles size={20} color="var(--primary)" />
                      AI Response
                    </div>
                    <span className="agent-endpoint-badge">
                      {agentEndpointMode === 'standard' ? 'POST /agent' : 'POST /agent/stream'}
                    </span>
                  </div>

                  <div className="agent-response-actions">
                    {agentAnswer && (
                      <>
                        <div className="chunk-view-segmented" role="group" aria-label="Response display mode">
                          <button
                            type="button"
                            className={`chunk-view-btn ${agentViewMode === 'rendered' ? 'active' : ''}`}
                            onClick={() => setAgentViewMode('rendered')}
                            title="Formatted view with rich typography and interactive citations"
                          >
                            <AlignLeft size={13} />
                            <span>Formatted</span>
                          </button>
                          <button
                            type="button"
                            className={`chunk-view-btn ${agentViewMode === 'raw' ? 'active' : ''}`}
                            onClick={() => setAgentViewMode('raw')}
                            title="Raw Markdown output"
                          >
                            <Code2 size={13} />
                            <span>Markdown</span>
                          </button>
                        </div>

                        <button
                          type="button"
                          className={`chunk-copy-btn ${agentCopied ? 'copied' : ''}`}
                          onClick={handleCopyAgentAnswer}
                          title={agentCopied ? 'Copied!' : 'Copy AI response'}
                        >
                          {agentCopied ? <Check size={13} /> : <Copy size={13} />}
                          <span>{agentCopied ? 'Copied' : 'Copy'}</span>
                        </button>
                      </>
                    )}

                    {agentEvents.length > 0 && (
                      <button
                        type="button"
                        className="btn-event-stream"
                        onClick={() => setShowStreamEvents(!showStreamEvents)}
                      >
                        <Terminal size={15} />
                        {showStreamEvents ? 'Hide Stream Events' : 'Stream Events'}
                      </button>
                    )}
                  </div>
                </div>

                {agentLoading && !agentAnswer ? (
                  <div style={{ padding: '1.5rem', textAlign: 'center', color: 'var(--text-muted)' }}>
                    <RefreshCw className="spinner" size={24} style={{ marginBottom: '0.5rem', color: 'var(--primary)' }} />
                    <p style={{ fontSize: '0.9rem' }}>
                      {agentEndpointMode === 'standard'
                        ? 'Awaiting grounded JSON response from /agent...'
                        : 'Streaming message events from agent state graph...'}
                    </p>
                  </div>
                ) : (
                  <AgentMarkdownView
                    markdown={agentAnswer}
                    events={agentEvents}
                    viewMode={agentViewMode}
                    queryTerms={extractQueryTerms(submittedAgentQuery)}
                  />
                )}

                {/* Pretty Print Event Stream Drawer / View */}
                {showStreamEvents && (
                  <PrettyPrintEventStream events={agentEvents} />
                )}
              </div>
            )}
          </div>
        )}

        {/* Manage Collections Tab View */}
        {activeTab === 'manage' && (
          <div className="glass-card">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
              <h2 className="card-title" style={{ margin: 0 }}>
                <Database size={22} color="var(--primary)" />
                Manage Collections
              </h2>
              <button onClick={fetchCollections} className="refresh-btn" title="Refresh collections">
                <RefreshCw size={16} className={apiStatus === 'checking' ? 'spinner' : ''} />
              </button>
            </div>
            <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginBottom: '1.5rem' }}>
              View stored vector store collections, check document chunk counts, or delete existing collections.
            </p>

            {collectionDetails.length === 0 ? (
              <div style={{ padding: '3rem 1.5rem', textAlign: 'center', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-md)', border: '1px dashed var(--border-color)' }}>
                <Database size={40} color="var(--text-muted)" style={{ marginBottom: '1rem', opacity: 0.5 }} />
                <p style={{ color: 'var(--text-muted)', fontSize: '0.95rem' }}>No vector store collections exist yet.</p>
                <button 
                  className="btn-primary" 
                  style={{ marginTop: '1rem', display: 'inline-flex', width: 'auto', padding: '0.6rem 1.2rem' }}
                  onClick={() => setActiveTab('ingest')}
                >
                  <Upload size={16} />
                  Ingest Documents to Create Collection
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
                            <Server size={20} />
                          </div>
                          <div>
                            <h3 style={{ fontSize: '1rem', fontWeight: 600, color: '#fff', margin: 0 }}>
                              {col.name}
                            </h3>
                            <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', margin: '2px 0 0 0' }}>
                              {col.count !== undefined ? `${col.count} document chunk(s) stored` : 'Chroma Collection'}
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
                                setActiveTab('query');
                              }}
                            >
                              <Search size={14} />
                              Query
                            </button>
                            <button
                              type="button"
                              className="tab-btn"
                              style={{ padding: '0.4rem 0.8rem', fontSize: '0.8rem' }}
                              onClick={() => {
                                setAgentCollection(col.name);
                                setActiveTab('agent');
                              }}
                            >
                              <Bot size={14} />
                              Ask Agent
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
                          justify: 'space-between',
                          gap: '1rem'
                        }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: '#fca5a5', fontSize: '0.85rem' }}>
                            <AlertTriangle size={18} style={{ flexShrink: 0 }} />
                            <span>Are you sure you want to delete collection <strong>"{col.name}"</strong>?</span>
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
                              Confirm Delete
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
