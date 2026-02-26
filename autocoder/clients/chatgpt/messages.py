from dataclasses import dataclass
from typing import Any, Dict, List
from python_core.clients.openai.types.roles import Role
from python_core.clients.openai.types.response_format import ResponseFormat


@dataclass
class Message:
    role: Role
    content: str
    

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role.value, "content": self.content}


@dataclass
class MessageList:
    messages: List[Message]

    def __iter__(self):
        return iter(self.messages)

    def __len__(self):
        return len(self.messages)

    def __getitem__(self, index):
        return self.messages[index]
    
    
    def to_dict(self) -> List[Dict[str, str]]:
        if type(self.messages) is list and type(self.messages[0]) is dict:
            return self.messages  # Already in dict form

        if type(self.messages) is list and type(self.messages[0]) is Message:
            return [m.to_dict() for m in self.messages]

        return [{"error": "Unknown message type"}]
    
    

    def add(self, role: Role, content: str) -> None:
        self.messages.append(Message(role, content))

    