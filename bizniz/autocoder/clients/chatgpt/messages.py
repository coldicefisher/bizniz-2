from dataclasses import dataclass
from typing import Any, Dict, List, Union
from autocoder.clients.chatgpt.types.roles import Role
from autocoder.clients.chatgpt.types.response_format import ResponseFormat


@dataclass
class Message:
    role: Role
    content: str
    

    def to_dict(self) -> Dict[str, Any]:
        _role = self.role.value if isinstance(self.role, Role) else str(self.role)
        return {"role": _role, "content": self.content}
        


@dataclass
class MessageList:
    messages: List[Message]
    input_tokens: float = 0.0
    output_tokens: float = 0.0

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
    
    
    def __str__(self):
        s = "Input Tokens: {}, Output Tokens: {}\n".format(self.input_tokens, self.output_tokens)
        
        for m in self.messages:
            role = None
            if isinstance(m.role, Role):
                role = m.role.value
            else:
                role = str(m.role)
                
            s += f"{role}: {m.content}\n"
        return s

    def __repr__(self):
        return self.__str__()

    def add(self, role: Role, content: str) -> None:
        self.messages.append(Message(role, content))

    
    
def normalize_messages(messages: Union[List[Dict[str, Any]], List[Message], MessageList]) -> List[Dict[str, Any]]:
    
        if messages is None:
            return None

        if isinstance(messages, MessageList):
            return messages.to_dict()

        if isinstance(messages, list):
            normalized = []
            for m in messages:
                if isinstance(m, Message):
                    normalized.append(m.to_dict())
                else:
                    normalized.append(m)
            return normalized

        return messages