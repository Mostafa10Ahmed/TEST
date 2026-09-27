import os
from tavily import TavilyClient
from bot_config import Config

class SearchTool:
    def __init__(self):
        self.api_key = Config.TAVILY_API_KEY
        if self.api_key:
            self.client = TavilyClient(api_key=self.api_key)
        else:
            self.client = None

    def search(self, query: str):
        """
        يبحث في الإنترنت عن المعلومات الحالية والأخبار.
        """
        if not self.client:
            return "خطأ: لم يتم ضبط TAVILY_API_KEY في ملف .env"
        
        try:
            # نحن نستخدم البحث "الذكي" المخصص للذكاء الاصطناعي
            response = self.client.search(query=query, search_depth="advanced", max_results=5)
            
            results = []
            for result in response.get('results', []):
                results.append(f"المصدر: {result['url']}\nالمحتوى: {result['content']}\n")
            
            return "\n---\n".join(results)
        except Exception as e:
            return f"حدث خطأ أثناء البحث: {str(e)}"
