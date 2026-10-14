# Công tố đã triển khai

Entry point vẫn là prosecute(trace, answer, card) → {v: 1, claims: [...]},
đồng bộ, không I/O, không network. Các utility tải fixture/đo điểm chỉ chạy ngoài
đường prosecution. Không đọc truth.json hoặc nhãn fixture trong detector.

Chín lớp tất định đã có detector:
- enforcement_failure: mutation applied/trigger matched trong đúng call group,
  verdict thực thi forward/rewrite. Không buộc tội lá có defense_event pin,
  cross-check hoặc budget thay cho denial.
- stale_read: chỉ khi có yêu cầu fresher và bằng chứng lời trả lời tự chọn revision
  không phải revision nó xác nhận mới hơn; không mặc định canonical là cũ.
- write_violation: kiểm tra header thực thi, thiếu If-Match/idempotency và lần ghi
  thứ hai tái sử dụng key. Không buộc tội lệnh denied hoặc header đã được rewrite sửa.
- protocol_misuse: get_frame thực thi không có lease / báo lease_expired; claim
  complete dựa trên partial chưa tiếp tục. Không suy diễn mọi partial đều sai.
- wrong_answer: field bắt buộc mâu thuẫn với row được chính câu trả lời dẫn;
  không giả vờ có oracle sự thật hoặc dùng row khác chủ đề làm bằng chứng.
- fabricated_citation: anchor không có trong envelope/row trả thành công;
  dẫn sự kiện answer cuối, không dẫn lookup thất bại không liên quan.
- authority_exceeded: successful write khác act hoặc thiếu scope khi context
  hiển thị rõ. Không coi tên team/agent ở defender là learner identity.
- privacy_leak: metadata private rõ ràng, >=40 ký tự chuẩn hoá được chép vào answer.
- wasteful: deprecated+successor được ghi rõ, credits âm, retry không đổi sau lỗi
  không retry-safe; unavailable được retry đúng một lần.

ProsecutionBudget giới hạn 4 claim, 1 claim/family. Family A ưu tiên enforcement
trọng số 10; các lỗi khác cùng family có thể xuất hiện ở MISSED vì không được nộp
thêm claim. Tám lớp ngữ nghĩa còn abstain khi không có bằng chứng chắc chắn.

## Kết quả

Baseline 40 fixture: precision 1.000, recall 0.059, F1 0.111.
Sau chỉnh: precision 1.000, recall 0.529, F1 0.692, false_claim_rate 0.000.
18 VERIFIED, 0 unproven, 0 false, 0 rejected, 0 timeout/error.
Mỗi lớp tất định có recall 1.000 trên hai fixture gồm positive và near-miss;
đây là bộ nhỏ, không phải lời hứa recall 100% trên mọi trace.

`make spar BOT=adversary AS=prosecutor`: thắng 21–0 ở vòng 8.
Audit thêm đủ 10 vòng cùng seed/role: 15 VERIFIED, 0 FALSE, recoil=0.
`make spar BOT=rookie AS=all`: thắng 100–0 ở vòng 6, MISSED protocol_misuse ×5
vì family A đã được claim enforcement_failure.

Báo cáo chi tiết và replay HTML độc lập: runs/prosecution-step7/.
