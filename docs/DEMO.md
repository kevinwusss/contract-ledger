# Five-minute synthetic demonstration

Run `python scripts/demo.py` with the repository's Python environment, then start the server as shown in the README. Log in as `demo-admin` with the password you chose. All files, companies and amounts below are fictional.

| Record | State | Currency | Receivable | Received | Invoiced | Remaining receivable |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| DEMO-001 / Example Client A | Reviewed | CNY | 10000.00 | 4000.00 | 3000.00 | 6000.00 |
| DEMO-002 / Example Client B | Reviewed | USD | 2000.00 | 500.00 | 0.00 | 1500.00 |
| DEMO-003 | Pending review | Unconfirmed business meaning | Unknown | None | None | Unknown |

DEMO-001 also has a payable amount of CNY 2500.00 and an actual payment of CNY 1000.00, leaving CNY 1500.00 unpaid. Its initial payment confirmation is stored after these entries.

1. On **合同库** (contract library), explain the separate currency totals and the unreviewed third document.
2. Open DEMO-001. Compare the synthetic source document with the confirmed metadata and individual financial entries.
3. Open **公司账款** (company accounts) and **款项台账** (transaction ledger). Follow a summary back to its contract and records.
4. Add a fictional receipt to DEMO-001 and observe that its earlier confirmation becomes stale. This changes the starting balances above.
5. Export an Excel ledger and archive/restore a demo document through **文件归档** (file archive). Explain why archive does not delete the original.

Useful labels: 导入合同 = upload; 已核对 = reviewed; 待核对 = pending; 收款 = receipt; 付款 = payment; 开票 = invoice; 未收 = not yet received; 未付 = not yet paid; 作废 = void.

To prepare screenshots or a screen recording, use only this demo instance. Clearly caption them "Synthetic demonstration". Never use a production account or a real contract for admissions screenshots.
