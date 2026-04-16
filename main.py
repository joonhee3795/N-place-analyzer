from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import re
import traceback 

app = FastAPI(title="네이버 플레이스 진단 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PlaceRequest(BaseModel):
    url: str

@app.get("/")
def read_root():
    return {"message": "네이버 플레이스 진단 API 서버가 정상 작동 중입니다."}

@app.post("/analyze")
def analyze_place(req: PlaceRequest):
    match = re.search(r'/(\d{6,})', req.url)
    if not match:
        raise HTTPException(status_code=400, detail="유효한 네이버 플레이스 URL이 아닙니다.")
    
    place_id = match.group(1)
    target_url = f"https://m.place.naver.com/place/{place_id}/home"
    scraped_data = {}
    
    try:
        with sync_playwright() as p:
            # [핵심 최적화] Render 무료 서버(리눅스)에서 메모리가 터지지 않도록 경량화 옵션을 대거 추가했습니다.
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage", # 메모리 부족 현상 해결의 핵심 키
                    "--disable-gpu",
                    "--single-process",
                    "--no-zygote"
                ]
            )
            
            context = browser.new_context(
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
            )
            page = context.new_page()
            
            # 클라우드 최적화: 불필요한 이미지 로딩 등을 기다리지 않고 뼈대만 로딩되면 바로 진행
            page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            
            try:
                page.wait_for_selector("text=리뷰", timeout=15000)
            except:
                pass 
            
            page.wait_for_timeout(2000)
            
            # --- 데이터 수집 ---
            og_title_element = page.locator('meta[property="og:title"]')
            og_title = og_title_element.get_attribute('content') if og_title_element.count() > 0 else ""
            page_title = page.title()
            
            if og_title and og_title != "네이버 플레이스":
                scraped_data['shop_name'] = og_title
            else:
                scraped_data['shop_name'] = page_title.split('-')[0].strip() if '-' in page_title else page_title
            
            body_text = page.locator("body").inner_text()
            
            visitor_match = re.search(r'방문자\s*리뷰\s*[\n\r]*\s*([\d,]+)', body_text)
            scraped_data['visitor_reviews'] = int(visitor_match.group(1).replace(',', '')) if visitor_match else 0
            
            blog_match = re.search(r'블로그\s*리뷰\s*[\n\r]*\s*([\d,]+)', body_text)
            scraped_data['blog_reviews'] = int(blog_match.group(1).replace(',', '')) if blog_match else 0
            
            og_desc_element = page.locator('meta[property="og:description"]')
            scraped_data['description'] = og_desc_element.get_attribute('content') if og_desc_element.count() > 0 else ""
                
            scraped_data['status'] = "success"
            
            browser.close()
            
    except Exception as e:
        scraped_data['status'] = "error"
        scraped_data['message'] = str(e)
        scraped_data['traceback'] = traceback.format_exc()

    # --- 점수 계산 ---
    total_score = 0
    metrics = {}

    if scraped_data.get('status') == 'success':
        v_rev = scraped_data.get('visitor_reviews', 0)
        b_rev = scraped_data.get('blog_reviews', 0)
        desc = scraped_data.get('description', '')

        traffic_score = min(20, int((v_rev / 1000) * 20)) if v_rev > 0 else 0
        traffic_status = "양호" if traffic_score >= 16 else "보통" if traffic_score >= 8 else "위험"
        metrics['traffic'] = {"score": traffic_score, "status": traffic_status, "label": "트래픽"}

        dwell_score = min(20, int((b_rev / 200) * 20)) if b_rev > 0 else 0
        dwell_status = "양호" if dwell_score >= 16 else "보통" if dwell_score >= 8 else "위험"
        metrics['dwell_time'] = {"score": dwell_score, "status": dwell_status, "label": "체류시간"}

        seo_score = min(20, int((len(desc) / 150) * 20)) if len(desc) > 0 else 0
        seo_status = "양호" if seo_score >= 16 else "보통" if seo_score >= 8 else "위험"
        metrics['seo'] = {"score": seo_score, "status": seo_status, "label": "검색적합도"}

        review_score = 0
        if v_rev > 0:
            ratio = b_rev / v_rev
            if 0.05 <= ratio <= 0.20:
                review_score = 20
            elif 0.02 <= ratio < 0.05 or 0.20 < ratio <= 0.30: 
                review_score = 14
            else: 
                review_score = 8
        review_status = "양호" if review_score >= 16 else "보통" if review_score >= 12 else "위험"
        metrics['review_quality'] = {"score": review_score, "status": review_status, "label": "리뷰품질"}

        save_score = int((traffic_score * 0.6) + (dwell_score * 0.4))
        save_status = "양호" if save_score >= 16 else "보통" if save_score >= 8 else "위험"
        metrics['save_share'] = {"score": save_score, "status": save_status, "label": "저장/공유"}

        total_score = traffic_score + dwell_score + seo_score + review_score + save_score
        total_score = min(100, total_score + 5) if total_score > 0 else 0

    return {
        "place_id": place_id,
        "raw_data": scraped_data,
        "analysis": {
            "total_score": total_score,
            "metrics": metrics
        },
        "message": "데이터 수집 및 분석 로직 실행 완료"
    }