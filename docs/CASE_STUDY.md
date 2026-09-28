# Project case study

## Problem

Small business contract administration combines unstructured documents with structured financial records. Searching files alone cannot establish which values have been confirmed, how much has been received, or why a financial entry changed. The project translates these requirements into a local application with explicit review and audit states.

## Approach

The application preserves original evidence, extracts candidate information locally and asks users to confirm business meaning. A relational database links contracts to counterparties and financial entries. Company-level summaries can be traced back to individual contracts and transactions. Exports retain record identifiers and file hashes.

## Technical learning demonstrated by the artefact

The code provides concrete material for discussing Python web development, SQL transactions, data validation, document processing, authentication, automated testing and reproducibility. The financial rules also illustrate how domain assumptions affect software correctness: missing and zero are different states, and a contractual balance alone does not establish a missed payment deadline.

## Critical reflection

The most important automation decision is deciding what should remain unconfirmed. Pretrained OCR reduces manual transcription but cannot establish contractual obligations. Human review remains necessary. A useful evaluation would measure extraction precision, correction frequency and time spent reviewing, rather than describing OCR as simply accurate.

The current Chinese interface reflects the original workflow context. The Windows-dependent OCR test and locally oriented deployment are portability limits. The extended schema also needs stronger migration testing before it can support broader claims.

## Applicant contribution

This document describes the software, not an independently verified biography. Before using it in an application, write a factual account of your own contribution: which requirements you defined, which code you wrote or reviewed, which tests you understood and ran, and how you used AI assistance. Support that account with actual examples and future commits. Do not invent historical commit activity, user counts, cost savings or model-training work.

## Relevance to further study

For a computing conversion application, discuss the transition from business requirements to data structures, routes, transactions and tests. For business analytics or information systems, focus on data quality, traceability and decision support. For applied AI, focus on evaluating pretrained OCR and the design of human review. The project is supporting evidence and does not establish eligibility or guarantee admission.
