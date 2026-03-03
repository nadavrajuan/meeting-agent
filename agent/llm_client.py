# agent/llm_client.py
"""Unified LLM client supporting OpenAI and Gemini."""

import json
import os
from typing import Any

import openai


class LLMClient:
    def __init__(self, provider: str = None, model: str = None):
        self.provider = provider or os.getenv("LLM_PROVIDER", "openai")
        if self.provider == "openai":
            self.client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o")
        elif self.provider == "gemini":
            import google.generativeai as genai
            genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
            self.model = model or os.getenv("GEMINI_MODEL", "gemini-1.5-pro")
            self.gemini_model = genai.GenerativeModel(self.model)
        else:
            raise ValueError(f"Unknown LLM provider: {self.provider}")

    def complete(self, prompt: str, system: str = None, json_mode: bool = False) -> str:
        """Run a completion and return the response text."""
        if self.provider == "openai":
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            kwargs = {"model": self.model, "messages": messages}
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            resp = self.client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content

        elif self.provider == "gemini":
            full_prompt = (system + "\n\n" + prompt) if system else prompt
            resp = self.gemini_model.generate_content(full_prompt)
            return resp.text

    def complete_json(self, prompt: str, system: str = None) -> dict:
        """Run completion and parse JSON response."""
        text = self.complete(prompt, system=system, json_mode=(self.provider == "openai"))
        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            text = text.rsplit("```", 1)[0]
        return json.loads(text)
